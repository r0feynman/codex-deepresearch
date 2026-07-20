"""Deterministic semantic angle planning and validation."""

from __future__ import annotations

import json
import math
import copy
import os
import re
import shlex
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence


SEMANTIC_PLANNER_SCHEMA_VERSION = "codex-deepresearch.semantic-planner.v0"
SEMANTIC_PLANNER_VALIDATION_FILENAME = "semantic_planner_validation.json"
SEMANTIC_EXPECTATION_ORACLE_FILENAME = "semantic_expectation_oracle.json"
SEMANTIC_PLAN_FILENAME = "semantic_plan.json"
SEMANTIC_PLAN_REVIEW_FILENAME = "semantic_plan_review.json"
SEMANTIC_PLANNER_CONVERGENCE_FILENAME = "semantic_planner_convergence.json"
SEMANTIC_PLAN_DELTA_FILENAME = "semantic_plan_delta.json"
SEMANTIC_MATERIALIZATION_DIFF_FILENAME = "semantic_materialization_diff.json"
SEMANTIC_REQUIREMENT_WAIVERS_FILENAME = "semantic_requirement_waivers.json"
SEMANTIC_RAW_DIRNAME = "semantic_planner_raw"
SEMANTIC_ORACLE_RAW_DIRNAME = "semantic_oracle_raw"
SEMANTIC_REVIEWER_RAW_DIRNAME = "semantic_reviewer_raw"
SEMANTIC_RAW_REQUEST_FILENAME = "planner_request.json"
SEMANTIC_RAW_RESPONSE_FILENAME = "planner_response.json"
SEMANTIC_ORACLE_RAW_REQUEST_FILENAME = "oracle_request.json"
SEMANTIC_ORACLE_RAW_RESPONSE_FILENAME = "oracle_response.json"
SEMANTIC_REVIEWER_RAW_REQUEST_FILENAME = "reviewer_request.json"
SEMANTIC_REVIEWER_RAW_RESPONSE_FILENAME = "reviewer_response.json"
PLANNER_MODE_CODEX_SEMANTIC = "codex_semantic"
PLANNER_MODE_HEURISTIC_TEMPLATE_FALLBACK = "heuristic_template_fallback"
PLANNER_MODE_MANUAL_ANGLES = "manual_angles"
PLANNER_MODE_FIXTURE = "fixture"
PLANNER_MODE_BLOCKED = "blocked"
BLOCKED_SEMANTIC_PLANNER_UNAVAILABLE = "blocked_semantic_planner_unavailable"
CODEX_SEMANTIC_ADAPTER_NAME = "codex_native_semantic_candidate_adapter"
CODEX_SEMANTIC_PROMPT_VERSION = "p3-sp2-candidate-v2"
CODEX_SEMANTIC_ORACLE_ADAPTER_NAME = "codex_native_semantic_expectation_oracle"
CODEX_SEMANTIC_ORACLE_PROMPT_VERSION = "p3-sp3-oracle-v1"
CODEX_SEMANTIC_REVIEWER_ADAPTER_NAME = "codex_native_semantic_fit_reviewer"
CODEX_SEMANTIC_REVIEWER_PROMPT_VERSION = "p3-sp3-reviewer-v1"
CODEX_SEMANTIC_PLANNER_COMMAND_ENV = "CODEX_DEEPRESEARCH_SEMANTIC_PLANNER_COMMAND"
CODEX_SEMANTIC_ORACLE_COMMAND_ENV = "CODEX_DEEPRESEARCH_SEMANTIC_ORACLE_COMMAND"
CODEX_SEMANTIC_REVIEWER_COMMAND_ENV = "CODEX_DEEPRESEARCH_SEMANTIC_REVIEWER_COMMAND"
CODEX_SEMANTIC_PLANNER_TIMEOUT_ENV = "CODEX_DEEPRESEARCH_SEMANTIC_PLANNER_TIMEOUT_SECONDS"
CODEX_SEMANTIC_ORACLE_TIMEOUT_ENV = "CODEX_DEEPRESEARCH_SEMANTIC_ORACLE_TIMEOUT_SECONDS"
CODEX_SEMANTIC_REVIEWER_TIMEOUT_ENV = "CODEX_DEEPRESEARCH_SEMANTIC_REVIEWER_TIMEOUT_SECONDS"
CODEX_SEMANTIC_ENABLE_DEFAULT_ADAPTER_ENV = (
    "CODEX_DEEPRESEARCH_ENABLE_DEFAULT_SEMANTIC_ADAPTER"
)
CODEX_SEMANTIC_DISABLE_DEFAULT_ADAPTER_ENV = (
    "CODEX_DEEPRESEARCH_DISABLE_DEFAULT_SEMANTIC_ADAPTER"
)
CODEX_SEMANTIC_ADAPTER_WORKDIR_ENV = "CODEX_DEEPRESEARCH_SEMANTIC_ADAPTER_WORKDIR"
CODEX_SEMANTIC_ADAPTER_INVOCATION_KIND = "codex_exec_json"
CODEX_SEMANTIC_ADAPTER_CAPACITY_RETRY_ATTEMPTS_ENV = (
    "CODEX_DEEPRESEARCH_SEMANTIC_ADAPTER_CAPACITY_RETRY_ATTEMPTS"
)
CODEX_SEMANTIC_ADAPTER_CAPACITY_RETRY_BACKOFF_SECONDS_ENV = (
    "CODEX_DEEPRESEARCH_SEMANTIC_ADAPTER_CAPACITY_RETRY_BACKOFF_SECONDS"
)
CODEX_SEMANTIC_ADAPTER_SCHEMA_DIRNAME = "semantic_adapter_schemas"
CODEX_SEMANTIC_PLANNER_SCHEMA_FILENAME = "planner.json"
CODEX_SEMANTIC_ORACLE_SCHEMA_FILENAME = "oracle.json"
CODEX_SEMANTIC_REVIEWER_SCHEMA_FILENAME = "reviewer.json"
CODEX_SEMANTIC_PLANNER_VALIDATION_MAX_ATTEMPTS_ENV = (
    "CODEX_DEEPRESEARCH_SEMANTIC_PLANNER_VALIDATION_MAX_ATTEMPTS"
)
SEMANTIC_FIT_SCORE_THRESHOLD = 9.0
SEMANTIC_DELTA_DISALLOWED_REPAIR_CATEGORIES = (
    "failed_semantic_fit",
    "hidden_template_use",
    "missing_non_negotiable_coverage",
    "task_count_failure",
    "subject_drift",
    "required_modality_omission",
    "source_quality_omission",
    "generic_task_content",
)
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
    "manifest_oracle_hash",
    "manifest_oracle_path",
    "manifest_oracle_fragment_id",
)
SEMANTIC_MATERIALIZATION_ALIGNMENT_FIELDS = (
    "query",
    "route",
    "freshness_requirement",
    "source_policy",
    "expected_source_types",
    "expected_visual_targets",
    "expected_artifacts",
    "success_criteria",
    "done_condition",
    "max_results",
    "max_sources",
    "max_images",
)
SEMANTIC_MATERIALIZATION_SEARCH_RESULT_FIELD_PREFIX = "semantic_task_"
SEMANTIC_MATERIALIZATION_PLAN_HASH_FIELD = "semantic_materialization_plan_hash"
SEMANTIC_MATERIALIZATION_SEARCH_RESULT_ALIGNMENT_FIELD_MAP = {
    field: f"{SEMANTIC_MATERIALIZATION_SEARCH_RESULT_FIELD_PREFIX}{field}"
    for field in SEMANTIC_MATERIALIZATION_ALIGNMENT_FIELDS
}
SEMANTIC_MATERIALIZATION_JSON_ARTIFACTS = {
    "research_tasks": "research_tasks.json",
    "search_tasks": "search_tasks.json",
    "visual_tasks": "visual_tasks.json",
    "visual_search_plan": "visual_search_plan.json",
}
SEMANTIC_MATERIALIZATION_JSONL_ARTIFACTS = {
    "search_results": "search_results.jsonl",
    "visual_candidates": "visual_candidates.jsonl",
    "image_fetch_status": "image_fetch_status.jsonl",
    "visual_observations": "visual_observations.jsonl",
    "subagent_assignments": "subagent_assignments.jsonl",
}

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
VISUAL_OPTIONAL_SUPPORT_MAX_TASK_RATIO = 0.25
VISUAL_OPTIONAL_SUPPORT_MAX_ANGLE_RATIO = 0.25
TEXT_ONLY_VISUAL_WORK_TEXT_PATTERNS = (
    ("vlm_analysis", r"\bvlm(?:[-_\s]+analysis)?\b"),
    ("visual_example", r"\bvisual[-_\s]+examples?\b"),
    ("visual_observation", r"\bvisual[-_\s]+observations?\b"),
    (
        "visual_work",
        r"\bvisual[-_\s]+(?:evidence|artifacts?|inspection|analysis|comparison|targets?|sources?|interpretation|review)\b",
    ),
    ("image", r"\bimages?\b"),
    ("photo", r"\bphotos?\b"),
    ("screenshot", r"\bscreenshots?\b"),
    ("chart", r"\bcharts?\b|\bflowcharts?\b"),
    ("diagram", r"\bdiagrams?\b"),
    ("figure", r"\bfigures?\b(?!\s+out\b)"),
)
TEXT_ONLY_VISUAL_WORK_NEGATION_PATTERN = re.compile(
    r"(?:\bno\b|\bnot\b|\bwithout\b|\bexclude(?:s|d|ing)?\b|"
    r"\bavoid(?:s|ed|ing)?\b|\bdo not\b|\bdon't\b)[\w\s-]{0,30}$"
)
TEXT_ONLY_ANGLE_VISUAL_WORK_FIELDS = (
    "evidence_need",
    "title",
    "research_question",
    "why_this_angle_matters",
    "included_scope",
    "expected_source_types",
    "expected_artifacts",
    "search_queries",
    "success_criteria",
    "report_section",
    "risk_or_contradiction_checks",
)
TEXT_ONLY_TASK_VISUAL_WORK_FIELDS = (
    "query",
    "source_policy",
    "expected_source_types",
    "expected_artifacts",
    "success_criteria",
    "done_condition",
)
SEMANTIC_TASK_MIN_SOURCES = 1
SEMANTIC_TASK_MAX_SOURCES = 5
SEMANTIC_MULTI_SOURCE_CAP_FIELDS = (
    "query",
    "source_policy",
    "expected_source_types",
    "success_criteria",
    "done_condition",
    "freshness_requirement",
)
SEMANTIC_MULTI_SOURCE_CAP_NEEDLES = {
    "comparison": (
        "compare",
        "comparison",
        "comparative",
        "contrast",
        "cross check",
        "cross-check",
        "cross verification",
        "cross-verify",
        "verify against",
        "reconcile",
        "between",
        "across",
        "mapping",
        "map",
        "\ube44\uad50",
        "\ub300\uc870",
        "\uad50\ucc28",
        "\uac80\uc99d",
        "\ub9e4\ud551",
        "\ub300\uc751",
    ),
    "official_record": (
        "official",
        "regulatory",
        "regulator",
        "primary source",
        "primary record",
        "government",
        "agency",
        "ministry",
        "public institution",
        "record",
        "database",
        "notice",
        "statute",
        "law",
        "\uacf5\uc2dd",
        "\uaddc\uc81c",
        "\uc815\ubd80",
        "\uacf5\uacf5\uae30\uad00",
        "\uae30\uad00",
        "\uae30\ub85d",
        "\ub370\uc774\ud130\ubca0\uc774\uc2a4",
        "\uacf5\uc9c0",
        "\ubc95\ub839",
        "\uace0\uc2dc",
        "\uc6d0\ubb38",
        "\ubcf4\ub3c4\uc790\ub8cc",
        "\ub9ac\ucf5c",
    ),
    "freshness": (
        "latest",
        "current",
        "recent",
        "freshness",
        "updated",
        "update",
        "amended",
        "amendment",
        "revision",
        "revised",
        "as of",
        "effective date",
        "published date",
        "\ucd5c\uc2e0",
        "\ud604\ud589",
        "\ud604\uc7ac",
        "\ucd5c\uadfc",
        "\uc2e0\uaddc",
        "\uac1c\uc815",
        "\uc815\uc815",
        "\ubcc0\uacbd",
        "\uc2dc\ud589\uc77c",
        "\uac8c\uc2dc\uc77c",
        "\uc774\ub825",
    ),
    "contradiction": (
        "contradiction",
        "contradictory",
        "conflict",
        "conflicting",
        "caveat",
        "counter evidence",
        "counter-evidence",
        "discrepancy",
        "inconsistency",
        "mismatch",
        "unknown",
        "uncertain",
        "uncertainty",
        "unresolved",
        "omission",
        "duplicate",
        "correction",
        "\uc0c1\ucda9",
        "\uc774\uc0c1",
        "\ubd88\uc77c\uce58",
        "\ucc28\uc774",
        "\ubbf8\ud655\uc815",
        "\ud55c\uacc4",
        "\ub204\ub77d",
        "\uc911\ubcf5",
        "\uc815\uc815",
        "\ubc18\ub840",
        "\uc8fc\uc758\uc0ac\ud56d",
    ),
}
SEMANTIC_SOURCE_FAMILY_POLICY_KEYS = (
    "required_source_quality",
    "quality_requirements",
    "required_source_types",
    "required_sources",
    "source_types",
)
MATERIAL_ORIGINAL_OVERLAP_LIMIT = 0.72
MATERIAL_PEER_OVERLAP_LIMIT = 0.84
SEMANTIC_RELEASE_MIN_ANGLE_OVERLAP_TOKENS = 2
SEMANTIC_RELEASE_MIN_ANGLE_UNIQUE_TOKENS = 4
SEMANTIC_ANGLE_NEAR_DUPLICATE_THRESHOLD = 0.85
SEMANTIC_RELEASE_GENERIC_ANGLE_TEXTS = {
    "primary source discovery",
    "find authoritative sources that directly answer the research question",
}
SEMANTIC_RELEASE_GENERIC_PLACEHOLDER_PATTERNS = (
    r"\bangle\s*\d+\b",
    r"\bangle_\d+\b",
    r"\bevidence\s+angle\b",
    r"\bsupport\s+angle\b",
)
SEMANTIC_RELEASE_GENERIC_TOKENS = {
    "and",
    "answer",
    "authoritative",
    "compare",
    "directly",
    "does",
    "discovery",
    "do",
    "evidence",
    "find",
    "for",
    "from",
    "how",
    "official",
    "primary",
    "public",
    "question",
    "research",
    "source",
    "sources",
    "support",
    "supports",
    "supporting",
    "that",
    "the",
    "what",
    "with",
    "which",
}
SEMANTIC_RELEASE_NON_SUBSTANTIVE_SUFFIX_TOKENS = {
    "current",
    "latest",
    "new",
    "overview",
    "recap",
    "recent",
    "revision",
    "revised",
    "status",
    "summary",
    "update",
    "updated",
    "updates",
}
KOREAN_SEMANTIC_PARTICLE_SUFFIXES = (
    "께서는",
    "에서는",
    "에게는",
    "으로는",
    "로는",
    "에는",
    "과는",
    "와는",
    "에서",
    "에게",
    "께서",
    "으로",
    "로",
    "부터",
    "까지",
    "처럼",
    "보다",
    "마다",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "의",
    "와",
    "과",
    "도",
    "만",
    "에",
)

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
    runner_source_budget: dict[str, Any] = field(default_factory=dict)
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
        data["runner_source_budget"] = _review_visible_runner_source_budget_metadata(
            self.runner_source_budget
        )
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
    diagnostics: Mapping[str, Any] | None = None,
) -> SemanticPlan:
    """Return a release-ineligible blocked semantic-planner-unavailable stub."""

    normalized_question = " ".join(question.strip().split())
    question_class = classify_question(normalized_question)
    diagnostic_payload = {
        **_fallback_diagnostics(PLANNER_MODE_BLOCKED),
        **dict(diagnostics or {}),
        "blocked_reason": reason,
    }
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
        diagnostics=diagnostic_payload,
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
    raw_request_payload: Mapping[str, Any] | None = None,
) -> SemanticPlan:
    """Build the P3-SP2 Codex-native semantic candidate plan.

    The adapter keeps the candidate release-ineligible until P3-SP3/P3-SP4
    reviewer and E2E gates make it eligible.
    """

    original_question = question.strip()
    raw_request = (
        dict(raw_request_payload)
        if isinstance(raw_request_payload, Mapping)
        else build_codex_semantic_raw_request(
            question=original_question,
            user_constraints=user_constraints or [],
            depth_preset=depth_preset,
            visual_preference=visual_preference,
            budget_cap=budget_cap or {},
            provided_sources=provided_sources or [],
            provided_images=provided_images or [],
        )
    )
    parsed_raw_response: dict[str, Any] | None = None
    previous_attempts: list[dict[str, Any]] = []
    max_attempts = _codex_semantic_planner_validation_max_attempts()
    for attempt in range(1, max_attempts + 1):
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
            raw_response["adapter_attempt"] = attempt
            raw_response["adapter_attempt_count"] = attempt
            if previous_attempts:
                raw_response["previous_adapter_attempts"] = list(previous_attempts)
            parsed_raw_response = raw_response
            candidate = _candidate_plan_from_adapter_response(raw_response)
            candidate, source_cap_normalizations = (
                _normalize_candidate_executable_source_caps(candidate)
            )
            source_cap_split_materializations: list[dict[str, Any]] = []
            broad_cardinality_materializations: list[dict[str, Any]] = []
            task_source_cap_materializations: list[dict[str, Any]] = []
            repair_materialization_allowed = _semantic_repair_materialization_allowed(
                raw_request
            )
            if repair_materialization_allowed:
                candidate, source_cap_split_materializations = (
                    _materialize_candidate_source_cap_splits(candidate)
                )
                candidate, broad_cardinality_materializations = (
                    _materialize_candidate_broad_cardinality(
                        candidate,
                        original_question=original_question,
                    )
                )
            candidate, source_cap_constraint_materializations = (
                _materialize_candidate_source_cap_constraints(
                    candidate,
                    source_cap_normalizations=source_cap_normalizations,
                )
            )
            if repair_materialization_allowed:
                candidate, task_source_cap_materializations = (
                    _materialize_candidate_task_source_cap_feasibility(candidate)
                )
            candidate, budget_cap_materializations = (
                _materialize_candidate_budget_caps(
                    candidate,
                    budget_cap=raw_request.get("budget_cap"),
                )
            )
            candidate, expected_evidence_materializations = (
                _materialize_candidate_expected_evidence(candidate)
            )
            candidate, visual_image_cap_materializations = (
                _materialize_candidate_visual_image_cap_feasibility(
                    candidate,
                    budget_cap=raw_request.get("budget_cap"),
                )
            )
            candidate, angle_title_materializations = (
                _materialize_candidate_angle_title_prompt_anchors(
                    candidate,
                    original_question=original_question,
                )
            )
            candidate, requirement_coverage_repairs = (
                _repair_candidate_requirement_coverage(candidate)
            )
            candidate, placeholder_selection_materializations = (
                _materialize_candidate_placeholder_selection_workflow(candidate)
            )
            if source_cap_normalizations:
                raw_response["candidate_plan"] = candidate
                raw_response["candidate_plan_source_cap_normalizations"] = (
                    source_cap_normalizations
                )
            if source_cap_split_materializations:
                raw_response["candidate_plan"] = candidate
                raw_response["candidate_plan_source_cap_split_materializations"] = (
                    source_cap_split_materializations
                )
            if broad_cardinality_materializations:
                raw_response["candidate_plan"] = candidate
                raw_response["candidate_plan_broad_cardinality_materializations"] = (
                    broad_cardinality_materializations
                )
            if source_cap_constraint_materializations:
                raw_response["candidate_plan"] = candidate
                raw_response[
                    "candidate_plan_source_cap_constraint_materializations"
                ] = source_cap_constraint_materializations
            if task_source_cap_materializations:
                raw_response["candidate_plan"] = candidate
                raw_response["candidate_plan_task_source_cap_materializations"] = (
                    task_source_cap_materializations
                )
            if budget_cap_materializations:
                raw_response["candidate_plan"] = candidate
                raw_response["candidate_plan_request_budget_materializations"] = (
                    budget_cap_materializations
                )
            if expected_evidence_materializations:
                raw_response["candidate_plan"] = candidate
                raw_response["candidate_plan_expected_evidence_materializations"] = (
                    expected_evidence_materializations
                )
            if visual_image_cap_materializations:
                raw_response["candidate_plan"] = candidate
                raw_response[
                    "candidate_plan_visual_image_cap_materializations"
                ] = visual_image_cap_materializations
            if angle_title_materializations:
                raw_response["candidate_plan"] = candidate
                raw_response["candidate_plan_angle_title_materializations"] = (
                    angle_title_materializations
                )
            if requirement_coverage_repairs:
                raw_response["candidate_plan"] = candidate
                raw_response["candidate_plan_requirement_coverage_repairs"] = (
                    requirement_coverage_repairs
                )
            if placeholder_selection_materializations:
                raw_response["candidate_plan"] = candidate
                raw_response[
                    "candidate_plan_placeholder_selection_materializations"
                ] = placeholder_selection_materializations
            candidate_validation = _codex_semantic_candidate_validation(
                original_question=original_question,
                candidate=candidate,
                visual_preference=str(raw_request.get("visual_preference") or "auto"),
                provided_images=_list(raw_request.get("provided_images")),
            )
            raw_response["candidate_validation"] = candidate_validation
            if candidate_validation.get("ok") is True:
                raw_response["adapter_candidate_attempts"] = list(previous_attempts) + [
                    _candidate_validation_attempt_record(
                        attempt=attempt,
                        validation=candidate_validation,
                        raw_response=raw_response,
                        candidate=candidate,
                        final_selection=True,
                    )
                ]
                break
            if attempt < max_attempts:
                retry_request = _codex_semantic_retry_raw_request(
                    raw_request=raw_request,
                    attempt=attempt + 1,
                    validation=candidate_validation,
                )
                previous_attempts.append(
                    _candidate_validation_retry_record(
                        attempt=attempt,
                        validation=candidate_validation,
                        raw_response=raw_response,
                        candidate=candidate,
                        retry_request=retry_request,
                    )
                )
                raw_response["adapter_candidate_attempts"] = list(previous_attempts)
                raw_request = retry_request
                continue
            raw_response["adapter_candidate_attempts"] = list(previous_attempts) + [
                _candidate_validation_attempt_record(
                    attempt=attempt,
                    validation=candidate_validation,
                    raw_response=raw_response,
                    candidate=candidate,
                    repair_inputs={
                        "terminal_reason": "max_attempts_exhausted",
                        "retry_source": "adapter_candidate_validation",
                    },
                    terminal_failure=True,
                )
            ]
            raise SemanticPlannerAdapterUnavailable(
                _candidate_validation_blocked_reason(candidate_validation)
            )
        except SemanticPlannerAdapterUnavailable as exc:
            return _blocked_codex_semantic_adapter_plan(
                question=original_question,
                raw_request=raw_request,
                raw_response=parsed_raw_response or adapter_response,
                reason=str(exc) or "Codex semantic planner adapter returned invalid output.",
                failure_category="adapter_invalid_response",
            )
    else:  # pragma: no cover - loop always returns or breaks
        return _blocked_codex_semantic_adapter_plan(
            question=original_question,
            raw_request=raw_request,
            reason="Codex semantic planner adapter returned no valid candidate.",
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
        "raw_request_hash": raw_request["adapter_request_hash"],
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
    expected_evidence_needs = _ordered_unique(angle.evidence_need for angle in angles)
    broad_question = _effective_broad_question(
        question_class=question_class,
        expected_needs=expected_evidence_needs,
        declared_broad=candidate["question_scope"] == "broad",
    )
    return SemanticPlan(
        schema_version=SEMANTIC_PLANNER_SCHEMA_VERSION,
        question_class=question_class,
        broad_question=broad_question,
        source="codex_semantic",
        expected_evidence_needs=expected_evidence_needs,
        angles=angles,
        intent_summary=str(candidate["intent_summary"]),
        domain_entities=list(candidate["domain_entities"]),
        constraints=list(candidate["constraints"]),
        runner_source_budget=(
            dict(candidate.get("runner_source_budget"))
            if isinstance(candidate.get("runner_source_budget"), Mapping)
            else {}
        ),
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
    raw_request_payload: Mapping[str, Any] | None = None,
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
        raw_request_payload=raw_request_payload,
    )


class SemanticPlannerAdapterUnavailable(RuntimeError):
    """Raised when no real Codex semantic planner response can be consumed."""


def build_codex_semantic_raw_request(
    *,
    question: str,
    user_constraints: Sequence[str] | None = None,
    depth_preset: str = "standard",
    visual_preference: str | None = None,
    budget_cap: Mapping[str, Any] | None = None,
    provided_sources: Sequence[Mapping[str, Any]] | None = None,
    provided_images: Sequence[Mapping[str, Any]] | None = None,
    run_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build the planner raw request before the planner adapter is invoked."""

    raw_request = _codex_semantic_raw_request(
        question=question,
        user_constraints=user_constraints or [],
        depth_preset=depth_preset,
        visual_preference=visual_preference,
        budget_cap=budget_cap or {},
        provided_sources=provided_sources or [],
        provided_images=provided_images or [],
    )
    if run_id:
        raw_request["run_id"] = run_id
    if created_at:
        raw_request["created_at"] = created_at
    raw_request["adapter_request_hash"] = _sha256_payload(raw_request)
    return raw_request


def invoke_codex_semantic_planner_adapter(
    request: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Invoke the configured Codex-native semantic planner boundary.

    Production code must not synthesize codex_semantic output locally or accept
    arbitrary subprocesses as Codex-native provenance. When the user has not
    supplied an explicit command, the installed plugin invokes the local Codex
    CLI with a checked JSON schema.
    """

    command = _semantic_adapter_command(
        command_env=CODEX_SEMANTIC_PLANNER_COMMAND_ENV,
        role_label="semantic planner",
    )
    if command is None:
        return None
    command_boundary = validate_codex_semantic_adapter_command(command)
    timeout_seconds = float(os.environ.get(CODEX_SEMANTIC_PLANNER_TIMEOUT_ENV, "300"))
    completed, retry_metadata = _run_codex_semantic_adapter_command_with_capacity_retry(
        command=command,
        request=request,
        timeout_seconds=timeout_seconds,
        role_label="semantic planner",
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
    _force_parent_raw_request_hash(
        provenance,
        request.get("adapter_request_hash"),
    )
    for key, value in codex_event_provenance.items():
        provenance.setdefault(key, value)
    if retry_metadata:
        provenance["adapter_retry_metadata"] = retry_metadata
    payload = dict(payload)
    if retry_metadata:
        payload["adapter_retry_metadata"] = retry_metadata
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
    diagnostic_payload = _blocked_codex_adapter_diagnostics(
        failure_category=failure_category,
        reason=reason,
        raw_response=raw_response,
    )
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
        "diagnostics": diagnostic_payload,
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
        "raw_request_hash": raw_request.get("adapter_request_hash"),
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
        diagnostics=diagnostic_payload,
    )


def _blocked_codex_adapter_diagnostics(
    *,
    failure_category: str,
    reason: str,
    raw_response: Mapping[str, Any] | None,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "failure_category": failure_category,
        "adapter_response_received": raw_response is not None,
        "adapter_invalid_response_reason": reason,
    }
    if not raw_response:
        return diagnostics
    candidate_validation = raw_response.get("candidate_validation")
    if isinstance(candidate_validation, Mapping):
        failures = candidate_validation.get("failures")
        failure_codes = [
            str(failure.get("code"))
            for failure in failures
            if isinstance(failure, Mapping) and failure.get("code")
        ] if isinstance(failures, list) else []
        diagnostics["candidate_validation"] = dict(candidate_validation)
        diagnostics["candidate_validation_failure_codes"] = failure_codes
        diagnostics["candidate_validation_failure_count"] = int(
            candidate_validation.get("failure_count") or len(failure_codes)
        )
    diagnostics["adapter_response_preview"] = _preview_text(
        json.dumps(dict(raw_response), ensure_ascii=False, sort_keys=True)
    )
    return diagnostics


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
    candidate_plan = copy.deepcopy(dict(candidate))
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


def _normalize_candidate_executable_source_caps(
    candidate: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized = copy.deepcopy(dict(candidate))
    raw_tasks = normalized.get("bounded_tasks")
    if not isinstance(raw_tasks, list):
        return normalized, []
    normalized_tasks: list[Any] = []
    normalizations: list[dict[str, Any]] = []
    for index, task in enumerate(raw_tasks, start=1):
        if not isinstance(task, Mapping):
            normalized_tasks.append(task)
            continue
        normalized_task = dict(task)
        current_cap = _semantic_task_source_cap_int(task.get("max_sources"))
        if current_cap is None:
            normalized_tasks.append(normalized_task)
            continue
        required_cap, reasons = _candidate_task_min_executable_sources(task)
        repaired_cap = min(
            SEMANTIC_TASK_MAX_SOURCES,
            max(SEMANTIC_TASK_MIN_SOURCES, current_cap, required_cap),
        )
        if repaired_cap != current_cap:
            normalized_task["max_sources"] = repaired_cap
            normalizations.append(
                {
                    "task_id": task.get("task_id"),
                    "task_index": index,
                    "previous_max_sources": current_cap,
                    "normalized_max_sources": repaired_cap,
                    "minimum_executable_sources": required_cap,
                    "reasons": reasons,
                }
            )
        normalized_tasks.append(normalized_task)
    normalized["bounded_tasks"] = normalized_tasks
    return normalized, normalizations


def _semantic_repair_materialization_allowed(raw_request: Mapping[str, Any]) -> bool:
    """Allow heavier plan repair only after convergence has reviewer/validator input."""

    return _semantic_task_source_cap_int(
        raw_request.get("semantic_convergence_attempt")
    ) is not None


def _materialize_candidate_source_cap_splits(
    candidate: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized = copy.deepcopy(dict(candidate))
    raw_tasks = normalized.get("bounded_tasks")
    if not isinstance(raw_tasks, list):
        return normalized, []

    existing_task_ids = {
        str(task.get("task_id") or "")
        for task in raw_tasks
        if isinstance(task, Mapping) and str(task.get("task_id") or "")
    }
    split_replacements: dict[str, list[str]] = {}
    materializations: list[dict[str, Any]] = []
    normalized_tasks: list[Any] = []
    mapping_task_count = sum(1 for task in raw_tasks if isinstance(task, Mapping))
    for index, task in enumerate(raw_tasks, start=1):
        if not isinstance(task, Mapping):
            normalized_tasks.append(task)
            continue

        required_sources, source_reasons, source_counts = (
            _candidate_task_required_source_count(task)
        )
        max_images = _semantic_task_source_cap_int(task.get("max_images"))
        required_images, _image_counts = _candidate_task_required_image_count(task)
        if max_images is not None and required_images > max_images:
            normalized_tasks.append(dict(task))
            continue
        if required_sources <= SEMANTIC_TASK_MAX_SOURCES:
            normalized_tasks.append(dict(task))
            continue

        split_count = math.ceil(required_sources / SEMANTIC_TASK_MAX_SOURCES)
        if split_count <= 1:
            normalized_tasks.append(dict(task))
            continue
        if mapping_task_count + split_count - 1 > 40:
            normalized_tasks.append(dict(task))
            continue

        original_task_id = str(task.get("task_id") or f"task_{index:03d}")
        split_tasks: list[dict[str, Any]] = []
        for split_index in range(1, split_count + 1):
            split_task = _candidate_source_cap_split_task(
                task,
                original_task_id=original_task_id,
                split_index=split_index,
                split_count=split_count,
                required_sources=required_sources,
                existing_task_ids=existing_task_ids,
            )
            existing_task_ids.add(str(split_task["task_id"]))
            split_tasks.append(split_task)
        normalized_tasks.extend(split_tasks)
        split_task_ids = [str(split_task["task_id"]) for split_task in split_tasks]
        split_replacements[original_task_id] = split_task_ids
        materializations.append(
            {
                "field": "bounded_tasks",
                "materialization": "split_overbroad_source_requirement",
                "original_task_id": original_task_id,
                "original_task_index": index,
                "split_task_ids": split_task_ids,
                "split_task_count": split_count,
                "required_count": required_sources,
                "per_task_max_sources": SEMANTIC_TASK_MAX_SOURCES,
                "original_max_sources": _semantic_task_source_cap_int(
                    task.get("max_sources")
                ),
                "requirement_kind": _dominant_cap_requirement_kind(
                    source_counts,
                    fallback="source",
                    source_reasons=source_reasons,
                ),
                "explicit_requirement_counts": source_counts,
                "source_cap_reasons": source_reasons,
            }
        )

    if not split_replacements:
        normalized["bounded_tasks"] = normalized_tasks
        return normalized, []

    normalized["bounded_tasks"] = normalized_tasks
    normalized["requirement_coverage_map"] = _candidate_rewrite_split_task_coverage(
        normalized.get("requirement_coverage_map"),
        split_replacements=split_replacements,
    )
    normalized["constraints"] = _candidate_append_source_split_constraint(
        normalized.get("constraints"),
        materializations=materializations,
    )
    return normalized, materializations


def _candidate_source_cap_split_task(
    task: Mapping[str, Any],
    *,
    original_task_id: str,
    split_index: int,
    split_count: int,
    required_sources: int,
    existing_task_ids: set[str],
) -> dict[str, Any]:
    split_task = dict(task)
    split_task["task_id"] = _candidate_split_task_id(
        original_task_id,
        split_index=split_index,
        existing_task_ids=existing_task_ids,
    )
    subject = _candidate_source_split_subject(task)
    focus = _candidate_source_split_focus(task)
    split_task["query"] = (
        f"{subject} {focus} source group {split_index} of {split_count}"
    )
    split_task["max_sources"] = SEMANTIC_TASK_MAX_SOURCES
    if str(split_task.get("route") or "text_only") == "text_only":
        split_task["max_images"] = 0
        split_task["expected_visual_targets"] = []
    source_policy = split_task.get("source_policy")
    if isinstance(source_policy, Mapping):
        split_policy = dict(source_policy)
    else:
        split_policy = {"decision": "allowed", "flags": []}
    split_policy["source_group"] = {
        "index": split_index,
        "count": split_count,
        "original_task_id": original_task_id,
        "original_required_sources": required_sources,
    }
    split_task["source_policy"] = split_policy
    split_task["expected_artifacts"] = _ordered_unique(
        [
            *_candidate_source_split_scrub_list(
                _string_list(task.get("expected_artifacts"))
            ),
            f"source group {split_index} evidence notes",
        ]
    )
    split_task["success_criteria"] = _ordered_unique(
        [
            *_candidate_source_split_scrub_list(
                _string_list(task.get("success_criteria"))
            ),
            (
                f"Collect no more than {SEMANTIC_TASK_MAX_SOURCES} official or "
                "primary source records for this source group."
            ),
            (
                "Record source metadata, caveats, and unresolved contradictions "
                "without requiring cross-group synthesis inside this bounded task."
            ),
        ]
    )
    split_task["done_condition"] = (
        f"Stop when source group {split_index} has no more than "
        f"{SEMANTIC_TASK_MAX_SOURCES} source-backed findings, source metadata, "
        "and caveats; defer cross-group synthesis to the comparison deliverable."
    )
    return split_task


def _candidate_split_task_id(
    original_task_id: str,
    *,
    split_index: int,
    existing_task_ids: set[str],
) -> str:
    base = re.sub(r"[^A-Za-z0-9_]+", "_", original_task_id).strip("_")
    if not base:
        base = "task"
    candidate = f"{base}_srcgrp_{split_index:02d}"
    if candidate not in existing_task_ids:
        return candidate
    suffix = 2
    while f"{candidate}_{suffix}" in existing_task_ids:
        suffix += 1
    return f"{candidate}_{suffix}"


def _candidate_source_split_subject(task: Mapping[str, Any]) -> str:
    for value in (
        task.get("query"),
        task.get("done_condition"),
        task.get("expected_artifacts"),
    ):
        text = _candidate_source_split_scrub_text(
            json.dumps(value, ensure_ascii=False)
            if isinstance(value, (Mapping, list, tuple, set))
            else str(value or "")
        )
        words = [word for word in re.findall(r"[A-Za-z0-9.+#-]+", text) if len(word) > 2]
        if len(words) >= 3:
            return " ".join(words[:12])
    return "requested evidence"


def _candidate_source_split_focus(task: Mapping[str, Any]) -> str:
    text = _candidate_task_source_cap_text(task)
    if "migration" in text or "implementation" in text:
        return "implementation evidence"
    if "comparison" in text or "compare" in text:
        return "comparison evidence"
    if "official" in text or "regulatory" in text:
        return "official evidence"
    if "current" in text or "recent" in text or "latest" in text:
        return "freshness evidence"
    return "bounded evidence"


def _candidate_source_split_scrub_list(values: Sequence[str]) -> list[str]:
    return [
        scrubbed
        for value in values
        for scrubbed in [_candidate_source_split_scrub_text(value)]
        if scrubbed
    ]


def _candidate_source_split_scrub_text(value: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    count_pattern = (
        r"(?:[1-9]\d*|one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve|thirteen|fourteen|fifteen|twenty)"
    )
    counted_nouns = (
        "source",
        "sources",
        "official source",
        "official sources",
        "primary source",
        "primary sources",
        "regulatory source",
        "regulatory sources",
        "source record",
        "source records",
        "vendor",
        "vendors",
        "provider",
        "providers",
        "jurisdiction",
        "jurisdictions",
        "source-backed artifact",
        "source-backed artifacts",
    )
    noun_pattern = "|".join(
        re.escape(noun) for noun in sorted(counted_nouns, key=len, reverse=True)
    )
    text = re.sub(
        rf"\b(?:at least|minimum of|no fewer than|compare|across|from|cover|collect|inspect|analyze|review|use|include)?\s*"
        rf"{count_pattern}\s+(?:distinct\s+|different\s+|representative\s+|official\s+|primary\s+|regulatory\s+)?(?:{noun_pattern})\b",
        "source group evidence",
        text,
        flags=re.IGNORECASE,
    )
    return " ".join(text.split())


def _candidate_rewrite_split_task_coverage(
    raw_coverage: Any,
    *,
    split_replacements: Mapping[str, Sequence[str]],
) -> Any:
    if not isinstance(raw_coverage, list):
        return raw_coverage
    rewritten: list[Any] = []
    for requirement in raw_coverage:
        if not isinstance(requirement, Mapping):
            rewritten.append(requirement)
            continue
        repaired = dict(requirement)
        covered_task_ids: list[str] = []
        for task_id in _string_list(requirement.get("covered_by_task_ids")):
            replacements = split_replacements.get(task_id)
            if replacements:
                covered_task_ids.extend(str(item) for item in replacements)
            else:
                covered_task_ids.append(task_id)
        if covered_task_ids:
            repaired["covered_by_task_ids"] = _ordered_unique(covered_task_ids)
        rewritten.append(repaired)
    return rewritten


def _candidate_append_source_split_constraint(
    raw_constraints: Any,
    *,
    materializations: Sequence[Mapping[str, Any]],
) -> Any:
    if not isinstance(raw_constraints, list):
        return raw_constraints
    split_count = sum(
        int(materialization.get("split_task_count") or 0)
        for materialization in materializations
    )
    original_ids = [
        str(materialization.get("original_task_id"))
        for materialization in materializations
        if materialization.get("original_task_id")
    ]
    return [
        *raw_constraints,
        (
            "Over-broad source obligations were split before fan-out: "
            f"{', '.join(original_ids)} now materialize as {split_count} "
            f"bounded source-group tasks, each with max_sources<={SEMANTIC_TASK_MAX_SOURCES}. "
            "Synthesis must combine the source groups without raising any per-task cap."
        ),
    ]


def _materialize_candidate_broad_cardinality(
    candidate: Mapping[str, Any],
    *,
    original_question: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized = copy.deepcopy(dict(candidate))
    raw_angles = normalized.get("angles")
    raw_tasks = normalized.get("bounded_tasks")
    if not isinstance(raw_angles, list) or not isinstance(raw_tasks, list):
        return normalized, []

    angles = [dict(angle) for angle in raw_angles if isinstance(angle, Mapping)]
    tasks = [dict(task) for task in raw_tasks if isinstance(task, Mapping)]
    if not angles or not tasks or len(angles) > 8 or len(tasks) > 40:
        return normalized, []

    requirements = [
        requirement
        for requirement in _list(normalized.get("requirement_coverage_map"))
        if isinstance(requirement, Mapping)
    ]
    question_class = _candidate_validation_question_class(
        plan=normalized,
        angles=angles,
        requirements=requirements,
    )
    classified_question = classify_question(original_question)
    if question_class == _CLASS_GENERAL and classified_question != _CLASS_GENERAL:
        question_class = classified_question
    elif (
        question_class == _CLASS_POLICY
        and classified_question in {_CLASS_IMPLEMENTATION, _CLASS_TECHNICAL}
    ):
        question_class = classified_question
    expected_needs = _candidate_validation_expected_needs(
        plan=normalized,
        angles=angles,
    )
    effective_broad = _effective_broad_question(
        question_class=question_class,
        expected_needs=expected_needs,
        declared_broad=str(normalized.get("question_scope") or "") == "broad",
    )
    if not effective_broad and classified_question != _CLASS_IMPLEMENTATION:
        return normalized, []
    if len(angles) >= 5 and len(tasks) >= 20:
        if str(normalized.get("question_scope") or "") != "broad":
            normalized["question_scope"] = "broad"
            normalized.setdefault("question_class", question_class)
            return normalized, [
                {
                    "field": "question_scope",
                    "materialization": "promoted_effective_broad_question_scope",
                    "previous_question_scope": candidate.get("question_scope"),
                    "question_class": question_class,
                }
            ]
        return normalized, []

    materializations: list[dict[str, Any]] = []
    previous_scope = normalized.get("question_scope")
    if previous_scope != "broad":
        normalized["question_scope"] = "broad"
        materializations.append(
            {
                "field": "question_scope",
                "materialization": "promoted_effective_broad_question_scope",
                "previous_question_scope": previous_scope,
                "question_class": question_class,
            }
        )
    normalized["question_class"] = question_class

    existing_angle_ids = {
        str(angle.get("angle_id") or "")
        for angle in angles
        if str(angle.get("angle_id") or "")
    }
    added_angle_ids: list[str] = []
    while len(angles) < 5:
        angle = _candidate_broad_cardinality_angle(
            original_question=original_question,
            question_class=question_class,
            existing_angles=angles,
            existing_angle_ids=existing_angle_ids,
        )
        existing_angle_ids.add(str(angle["angle_id"]))
        added_angle_ids.append(str(angle["angle_id"]))
        angles.append(angle)

    existing_task_ids = {
        str(task.get("task_id") or "")
        for task in tasks
        if str(task.get("task_id") or "")
    }
    tasks_by_angle = Counter(str(task.get("angle_id") or "") for task in tasks)
    added_task_ids: list[str] = []
    task_sequence = 1
    while len(tasks) < 20 or any(
        tasks_by_angle[str(angle.get("angle_id") or "")] < 2 for angle in angles
    ):
        if len(tasks) >= 40:
            break
        angle = _candidate_broad_cardinality_next_angle(
            angles=angles,
            tasks_by_angle=tasks_by_angle,
            sequence=task_sequence,
        )
        task = _candidate_broad_cardinality_task(
            original_question=original_question,
            angle=angle,
            existing_tasks=tasks,
            existing_task_ids=existing_task_ids,
            sequence=task_sequence,
        )
        existing_task_ids.add(str(task["task_id"]))
        added_task_ids.append(str(task["task_id"]))
        tasks.append(task)
        tasks_by_angle[str(task["angle_id"])] += 1
        task_sequence += 1

    normalized["angles"] = [
        _candidate_record if isinstance(_candidate_record, Mapping) else _candidate_record
        for _candidate_record in raw_angles
        if not isinstance(_candidate_record, Mapping)
    ] + angles
    normalized["bounded_tasks"] = [
        _candidate_record if isinstance(_candidate_record, Mapping) else _candidate_record
        for _candidate_record in raw_tasks
        if not isinstance(_candidate_record, Mapping)
    ] + tasks
    if added_angle_ids:
        normalized["requirement_coverage_map"] = _candidate_add_angle_coverage(
            normalized.get("requirement_coverage_map"),
            added_angle_ids=added_angle_ids,
        )
    if added_task_ids:
        normalized["requirement_coverage_map"] = _candidate_add_task_coverage(
            normalized.get("requirement_coverage_map"),
            added_task_ids=added_task_ids,
        )
    if added_angle_ids or added_task_ids:
        normalized["decomposition_strategy"] = (
            str(normalized.get("decomposition_strategy") or "").rstrip()
            + " Broad-question cardinality was materialized before validation so "
            "the candidate preserves 5-8 semantic angles and 20-40 bounded tasks."
        ).strip()
        materializations.append(
            {
                "field": "angles,bounded_tasks",
                "materialization": "expanded_effective_broad_question_cardinality",
                "previous_angle_count": len(angles) - len(added_angle_ids),
                "previous_task_count": len(tasks) - len(added_task_ids),
                "materialized_angle_count": len(angles),
                "materialized_task_count": len(tasks),
                "added_angle_ids": added_angle_ids,
                "added_task_ids": added_task_ids,
                "question_class": question_class,
            }
        )
    return normalized, materializations


def _candidate_broad_cardinality_angle(
    *,
    original_question: str,
    question_class: str,
    existing_angles: Sequence[Mapping[str, Any]],
    existing_angle_ids: set[str],
) -> dict[str, Any]:
    angle_id = _candidate_next_angle_id(existing_angle_ids)
    evidence_need = _candidate_broad_missing_evidence_need(
        existing_angles,
        question_class=question_class,
    )
    anchor = _candidate_prompt_anchor_phrase(original_question)
    source_types = _candidate_preferred_source_types(existing_angles)
    route = "text_only"
    expected_visual_targets: list[str] = []
    return {
        "angle_id": angle_id,
        "title": f"{anchor.title()} {_candidate_evidence_need_label(evidence_need)}",
        "research_question": (
            f"Which source-backed evidence changes the assessment of {anchor}?"
        ),
        "why_this_angle_matters": (
            f"This angle preserves a distinct broad-question evidence need for {original_question}."
        ),
        "included_scope": [original_question],
        "excluded_scope": ["Do not substitute generic or unrelated implementation material."],
        "route": route,
        "evidence_need": evidence_need,
        "expected_source_types": source_types,
        "expected_visual_targets": expected_visual_targets,
        "expected_artifacts": [
            f"{anchor} {_candidate_evidence_need_label(evidence_need).lower()} notes",
            f"{anchor} evidence caveats",
        ],
        "search_queries": [
            f"{original_question} {_candidate_evidence_need_label(evidence_need).lower()} source evidence"
        ],
        "success_criteria": [
            f"Findings must directly address {original_question}.",
            "Claims must cite source metadata and record caveats or unknowns.",
        ],
        "report_section": f"{anchor.title()} {_candidate_evidence_need_label(evidence_need)}",
        "risk_or_contradiction_checks": [
            "Check stale, contradictory, deprecated, or version-specific evidence.",
        ],
    }


def _candidate_next_angle_id(existing_angle_ids: set[str]) -> str:
    index = 1
    while f"angle_{index:03d}" in existing_angle_ids:
        index += 1
    return f"angle_{index:03d}"


def _candidate_broad_missing_evidence_need(
    existing_angles: Sequence[Mapping[str, Any]],
    *,
    question_class: str,
) -> str:
    existing = {
        str(angle.get("evidence_need") or "")
        for angle in existing_angles
        if str(angle.get("evidence_need") or "")
    }
    needs_by_class = {
        _CLASS_IMPLEMENTATION: (
            "primary_source",
            "implementation_detail",
            "recent_change",
            "failure_pattern",
            "comparative_analysis",
            "risk_or_guardrail",
            "counter_evidence",
        ),
        _CLASS_TECHNICAL: (
            "official_source",
            "recent_change",
            "implementation_detail",
            "failure_pattern",
            "risk_or_guardrail",
            "counter_evidence",
        ),
        _CLASS_POLICY: (
            "official_source",
            "policy_or_legal",
            "risk_or_guardrail",
            "comparative_analysis",
            "counter_evidence",
        ),
    }
    for need in needs_by_class.get(
        question_class,
        (
            "primary_source",
            "comparative_analysis",
            "recent_change",
            "failure_pattern",
            "risk_or_guardrail",
            "counter_evidence",
        ),
    ):
        if need not in existing:
            return need
    return "counter_evidence"


def _candidate_prompt_anchor_phrase(original_question: str) -> str:
    records = _semantic_release_ordered_meaningful_token_records(original_question)
    display_tokens = [
        str(record.get("display") or record.get("token"))
        for record in records
        if str(record.get("display") or record.get("token") or "")
    ]
    if len(display_tokens) >= 4:
        return " ".join(display_tokens[:4])
    if display_tokens:
        return " ".join(display_tokens)
    fallback = " ".join(str(original_question or "requested subject").split()[:4])
    return fallback or "requested subject"


def _candidate_preferred_source_types(
    angles: Sequence[Mapping[str, Any]],
) -> list[str]:
    for angle in angles:
        values = _string_list(angle.get("expected_source_types"))
        if values:
            return values
    return ["official documentation", "primary sources"]


def _candidate_evidence_need_label(evidence_need: str) -> str:
    labels = {
        "primary_source": "Primary Evidence",
        "official_source": "Official Evidence",
        "implementation_detail": "Implementation Detail",
        "recent_change": "Recent Change Evidence",
        "failure_pattern": "Failure Pattern Evidence",
        "comparative_analysis": "Comparison Evidence",
        "risk_or_guardrail": "Risk Guardrail Evidence",
        "counter_evidence": "Counter Evidence",
        "policy_or_legal": "Policy Evidence",
        "user_workflow": "Workflow Evidence",
    }
    return labels.get(evidence_need, "Source Evidence")


def _candidate_broad_cardinality_next_angle(
    *,
    angles: Sequence[Mapping[str, Any]],
    tasks_by_angle: Mapping[str, int],
    sequence: int,
) -> Mapping[str, Any]:
    missing_minimum = [
        angle
        for angle in angles
        if tasks_by_angle[str(angle.get("angle_id") or "")] < 2
    ]
    if missing_minimum:
        return sorted(
            missing_minimum,
            key=lambda angle: str(angle.get("angle_id") or ""),
        )[0]
    return angles[(sequence - 1) % len(angles)]


def _candidate_broad_cardinality_task(
    *,
    original_question: str,
    angle: Mapping[str, Any],
    existing_tasks: Sequence[Mapping[str, Any]],
    existing_task_ids: set[str],
    sequence: int,
) -> dict[str, Any]:
    task_id = _candidate_next_task_id(existing_task_ids)
    angle_id = str(angle.get("angle_id") or "")
    base_task = next(
        (
            task
            for task in existing_tasks
            if str(task.get("angle_id") or "") == angle_id
        ),
        {},
    )
    route = str(angle.get("route") or base_task.get("route") or "text_only")
    max_sources = _semantic_task_source_cap_int(base_task.get("max_sources"))
    if max_sources is None:
        max_sources = 3
    max_sources = max(
        SEMANTIC_TASK_MIN_SOURCES,
        min(SEMANTIC_TASK_MAX_SOURCES, max_sources),
    )
    max_images = _semantic_task_source_cap_int(base_task.get("max_images")) or 0
    if route == "text_only":
        max_images = 0
    focus = _candidate_broad_task_focus(sequence)
    angle_title = str(angle.get("title") or angle_id)
    source_policy = (
        dict(base_task.get("source_policy"))
        if isinstance(base_task.get("source_policy"), Mapping)
        else {"decision": "allowed", "flags": []}
    )
    freshness = str(
        base_task.get("freshness_requirement")
        or ("recent" if "current" in original_question.lower() else "any")
    )
    return {
        "task_id": task_id,
        "angle_id": angle_id,
        "query": f"{original_question} {angle_title} {focus}",
        "route": route,
        "freshness_requirement": freshness,
        "source_policy": source_policy,
        "expected_source_types": _string_list(angle.get("expected_source_types"))
        or _string_list(base_task.get("expected_source_types"))
        or ["official documentation", "primary sources"],
        "expected_visual_targets": (
            _string_list(angle.get("expected_visual_targets"))
            if route != "text_only"
            else []
        ),
        "expected_artifacts": _ordered_unique(
            [
                *_string_list(angle.get("expected_artifacts")),
                f"{angle_title} {focus} notes",
            ]
        ),
        "success_criteria": _ordered_unique(
            [
                *_string_list(angle.get("success_criteria")),
                f"Task findings must directly address {original_question}.",
                "Use source-backed evidence and record caveats, conflicts, or unknowns.",
            ]
        ),
        "max_results": base_task.get("max_results", 8),
        "max_sources": max_sources,
        "max_images": max_images,
        "done_condition": (
            "Stop when this bounded task has source-backed findings, evidence "
            "metadata, caveats, and unresolved items marked unknown."
        ),
    }


def _candidate_next_task_id(existing_task_ids: set[str]) -> str:
    index = 1
    while f"task_semantic_{index:03d}" in existing_task_ids:
        index += 1
    return f"task_semantic_{index:03d}"


def _candidate_broad_task_focus(sequence: int) -> str:
    focuses = (
        "official documentation baseline",
        "current behavior constraints",
        "migration hazard evidence",
        "version caveat cross-check",
        "remediation evidence",
        "counter-evidence review",
        "implementation boundary check",
        "synthesis readiness check",
    )
    return focuses[(sequence - 1) % len(focuses)]


def _candidate_add_angle_coverage(
    raw_coverage: Any,
    *,
    added_angle_ids: Sequence[str],
) -> Any:
    if not isinstance(raw_coverage, list):
        return raw_coverage
    updated: list[Any] = []
    for requirement in raw_coverage:
        if not isinstance(requirement, Mapping):
            updated.append(requirement)
            continue
        repaired = dict(requirement)
        repaired["covered_by_angle_ids"] = _ordered_unique(
            [
                *_string_list(requirement.get("covered_by_angle_ids")),
                *[str(angle_id) for angle_id in added_angle_ids],
            ]
        )
        updated.append(repaired)
    return updated


def _candidate_add_task_coverage(
    raw_coverage: Any,
    *,
    added_task_ids: Sequence[str],
) -> Any:
    if not isinstance(raw_coverage, list):
        return raw_coverage
    updated: list[Any] = []
    for requirement in raw_coverage:
        if not isinstance(requirement, Mapping):
            updated.append(requirement)
            continue
        repaired = dict(requirement)
        repaired["covered_by_task_ids"] = _ordered_unique(
            [
                *_string_list(requirement.get("covered_by_task_ids")),
                *[str(task_id) for task_id in added_task_ids],
            ]
        )
        updated.append(repaired)
    return updated


def _materialize_candidate_source_cap_constraints(
    candidate: Mapping[str, Any],
    *,
    source_cap_normalizations: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized = copy.deepcopy(dict(candidate))
    raw_constraints = normalized.get("constraints")
    if not isinstance(raw_constraints, list):
        return normalized, []

    tasks = [
        task
        for task in normalized.get("bounded_tasks") or []
        if isinstance(task, Mapping)
    ]
    caps = [
        cap
        for cap in (
            _semantic_task_source_cap_int(task.get("max_sources"))
            for task in tasks
        )
        if cap is not None
    ]
    if not caps:
        return normalized, []

    kept_constraints: list[Any] = []
    removed_constraints: list[str] = []
    conflict_records: list[dict[str, Any]] = []
    for constraint in raw_constraints:
        conflict = _candidate_source_cap_constraint_conflict_record(
            constraint,
            normalized_caps=caps,
        )
        if conflict:
            removed_constraints.append(str(constraint))
            conflict_records.append(conflict)
            continue
        kept_constraints.append(constraint)

    if not removed_constraints:
        return normalized, []

    min_cap = min(caps)
    max_cap = max(caps)
    total_cap = sum(caps)
    declared_global_budgets = [
        int(conflict["declared_source_budget"])
        for conflict in conflict_records
        if _semantic_task_source_cap_int(conflict.get("declared_source_budget")) is not None
    ]
    declared_global_budget = min(declared_global_budgets) if declared_global_budgets else None
    runner_source_budget: dict[str, Any] | None = None
    if declared_global_budget is not None:
        runner_source_budget = _candidate_runner_source_budget_record(
            declared_source_budget=declared_global_budget,
            task_max_sources_sum=total_cap,
            bounded_task_count=len(caps),
            removed_constraints=removed_constraints,
        )
        existing_runner_budget = normalized.get("runner_source_budget")
        if isinstance(existing_runner_budget, Mapping):
            merged_runner_budget = _review_visible_runner_source_budget_metadata(
                existing_runner_budget
            )
            merged_runner_budget.update(runner_source_budget)
            runner_source_budget = merged_runner_budget
        normalized["runner_source_budget"] = runner_source_budget
        replacement = (
            "Executable source caps are task-specific: bounded_tasks.max_sources "
            "is authoritative after source-cap consistency repair. The declared "
            "global source budget is preserved as a runner-level unique-source "
            f"reuse budget: max_unique_sources={declared_global_budget}. The "
            f"combined per-task source ceilings total {total_cap}, so execution must "
            "reuse sources through a shared source pool and must not treat "
            "per-task ceilings as additive permission for unique source retrievals."
        )
    else:
        replacement = (
            "Executable source caps are task-specific: bounded_tasks.max_sources "
            f"is authoritative after source-cap consistency repair and ranges from "
            f"{min_cap} to {max_cap}. Runner-level source and image budgets remain "
            "authoritative execution limits; tasks must reuse sources and record "
            "cap-limited caveats rather than exceed confirmed run budgets."
        )
    kept_constraints.append(replacement)
    normalized["constraints"] = kept_constraints
    materialization = {
        "field": "constraints",
        "materialization": "replaced_stale_source_cap_constraint",
        "removed_constraints": removed_constraints,
        "normalized_min_sources": min_cap,
        "normalized_max_sources": max_cap,
        "normalized_total_sources": total_cap,
        "normalized_task_count": len(source_cap_normalizations),
        "bounded_task_count": len(caps),
        "conflicts": conflict_records,
        "replacement_constraint": replacement,
    }
    if declared_global_budget is not None:
        materialization.update(
            {
                "preserved_declared_source_budget": declared_global_budget,
                "runner_source_budget": copy.deepcopy(runner_source_budget),
            }
        )
    return normalized, [materialization]


def _candidate_source_cap_constraint_conflict_record(
    constraint: Any,
    *,
    normalized_caps: Sequence[int],
) -> dict[str, Any] | None:
    if (
        "bounded_tasks.max_sources is authoritative" in str(constraint).lower()
        and _candidate_declared_global_source_budget(constraint) is None
    ):
        return None
    global_budget = _candidate_declared_global_source_budget(constraint)
    total_cap = sum(normalized_caps)
    global_budget_conflict = (
        global_budget is not None
        and total_cap > global_budget
        and not _candidate_constraint_preserves_executable_source_budget(
            constraint,
            declared_source_budget=global_budget,
        )
    )
    if _candidate_source_cap_constraint_conflicts_with_normalization(
        constraint,
        normalized_caps=normalized_caps,
    ):
        record = {
            "conflict_type": "per_task_single_source_cap",
            "max_task_cap": max(normalized_caps) if normalized_caps else None,
        }
        if global_budget_conflict:
            record.update(
                {
                    "declared_source_budget": global_budget,
                    "task_max_sources_sum": total_cap,
                    "bounded_task_count": len(normalized_caps),
                }
            )
        return record
    if global_budget_conflict:
        return {
            "conflict_type": "global_source_budget_less_than_task_caps",
            "declared_source_budget": global_budget,
            "task_max_sources_sum": total_cap,
            "bounded_task_count": len(normalized_caps),
        }
    return None


def _candidate_runner_source_budget_record(
    *,
    declared_source_budget: int,
    task_max_sources_sum: int,
    bounded_task_count: int,
    removed_constraints: Sequence[str],
) -> dict[str, Any]:
    return {
        "budget_type": "runner_level_unique_source_reuse",
        "max_unique_sources": declared_source_budget,
        "declared_source_budget": declared_source_budget,
        "task_max_sources_sum": task_max_sources_sum,
        "bounded_task_count": bounded_task_count,
        "reuse_required": task_max_sources_sum > declared_source_budget,
        "allocation_strategy": "shared_source_pool",
        "enforcement_scope": "run",
        "materialized_constraint_count": len(removed_constraints),
    }


def _candidate_constraint_preserves_executable_source_budget(
    constraint: Any,
    *,
    declared_source_budget: int,
) -> bool:
    text = str(constraint)
    lowered = text.lower()
    if str(declared_source_budget) not in lowered:
        return False
    has_runner_scope = (
        "runner-level" in lowered
        or "runner level" in lowered
        or "run-level" in lowered
        or "execution" in lowered
        or "executable" in lowered
    )
    has_reuse_budget = (
        "max_unique_sources" in lowered
        or "unique-source" in lowered
        or "unique source" in lowered
        or "reuse" in lowered
        or "shared source pool" in lowered
    )
    if not (has_runner_scope and has_reuse_budget):
        return False
    stale_sum_patterns = (
        r"\bsum\s*(?:of\s*)?(?:bounded_tasks\.)?max_sources\s*(?:<=|≤|=|is|must not exceed|not exceed|at most|no more than)",
        r"\btask\s+max_sources\s+sum\s*(?:<=|≤|=|is|must not exceed|not exceed|at most|no more than)",
    )
    return not any(re.search(pattern, lowered) for pattern in stale_sum_patterns)


def _candidate_source_cap_constraint_conflicts_with_normalization(
    constraint: Any,
    *,
    normalized_caps: Sequence[int],
) -> bool:
    text = str(constraint)
    lowered = text.lower()
    max_normalized_cap = max(normalized_caps) if normalized_caps else 1
    if max_normalized_cap <= 1:
        return False
    if "bounded_tasks.max_sources is authoritative" in lowered:
        return False
    mentions_source_cap = (
        "max_sources" in lowered
        or "source cap" in lowered
        or "source budget" in lowered
        or "per-task source" in lowered
        or "출처 예산" in text
        or ("출처" in text and "제한" in text)
        or ("출처" in text and "최대" in text)
    )
    if not mentions_source_cap:
        return False
    single_source_patterns = (
        r"\bmax_sources\b[^0-9]{0,80}\b1\b",
        r"\b1\b[^0-9]{0,80}\bmax_sources\b",
        r"\bper[- ]?task\b[^0-9]{0,80}\b1\b",
        r"\bper[- ]?task\b[^.\n;]{0,80}\bone\b[^.\n;]{0,40}\bsource\b",
        r"\beach\b[^0-9]{0,80}\btask\b[^0-9]{0,80}\b1\b",
        r"\beach\b[^.\n;]{0,80}\btask\b[^.\n;]{0,80}\bone\b[^.\n;]{0,40}\bsource\b",
        r"\bcapped\b[^.\n;]{0,40}\bone\b[^.\n;]{0,40}\bsource\b",
        r"\bone\b[^.\n;]{0,40}\bdecisive\b[^.\n;]{0,40}\bsource\b",
        r"\bone\b[^.\n;]{0,40}\bsource\b[^.\n;]{0,40}\bper[- ]?task\b",
        r"\bsingle[- ]?source\b",
        r"각\s*bounded\s*task[^0-9]{0,80}1",
        r"max_sources[^0-9]{0,80}1",
    )
    return any(
        re.search(pattern, lowered if pattern.isascii() else text)
        for pattern in single_source_patterns
    )


def _materialize_candidate_task_source_cap_feasibility(
    candidate: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized = copy.deepcopy(dict(candidate))
    raw_tasks = normalized.get("bounded_tasks")
    if not isinstance(raw_tasks, list):
        return normalized, []

    angles_by_id = {
        str(angle.get("angle_id") or ""): angle
        for angle in _list(normalized.get("angles"))
        if isinstance(angle, Mapping)
    }
    normalized_tasks: list[Any] = []
    materializations: list[dict[str, Any]] = []
    for index, task in enumerate(raw_tasks, start=1):
        if not isinstance(task, Mapping):
            normalized_tasks.append(task)
            continue
        normalized_task = dict(task)
        max_sources = _semantic_task_source_cap_int(normalized_task.get("max_sources"))
        if max_sources is None or max_sources < SEMANTIC_TASK_MIN_SOURCES:
            normalized_tasks.append(normalized_task)
            continue
        max_images = _semantic_task_source_cap_int(normalized_task.get("max_images"))
        required_images, _image_counts = _candidate_task_required_image_count(
            normalized_task
        )
        if max_images is not None and required_images > max_images:
            normalized_tasks.append(normalized_task)
            continue
        required_sources, source_reasons, source_counts = (
            _candidate_task_required_source_count(normalized_task)
        )
        if required_sources <= max_sources:
            normalized_tasks.append(normalized_task)
            continue

        angle = angles_by_id.get(str(normalized_task.get("angle_id") or ""))
        angle_title = (
            str(angle.get("title") or "").strip()
            if isinstance(angle, Mapping)
            else ""
        )
        subject = _candidate_task_subject_anchor(normalized_task, fallback=angle_title)
        if not subject:
            subject = "the requested investigation"
        normalized_task["query"] = (
            f"{subject} shared-source-pool synthesis for "
            f"{angle_title or 'the semantic angle'}"
        )
        normalized_task["success_criteria"] = [
            (
                f"Use no more than {max_sources} new source records in this bounded "
                "task; reuse upstream evidence from the shared source pool for wider "
                "synthesis."
            ),
            (
                "Preserve every requested comparison, caveat, status, and remediation "
                "field without requiring this task to fetch more sources than its cap."
            ),
        ]
        normalized_task["done_condition"] = (
            "Complete when capped new evidence plus upstream shared-source-pool "
            "findings are mapped into the requested synthesis, with unresolved gaps "
            "marked as caveats instead of expanding this task's source cap."
        )
        normalized_task["source_pool_reuse_required"] = True
        normalized_task["source_pool_reuse_note"] = (
            "The task previously required more distinct source records than "
            "bounded_tasks.max_sources permits. It now reuses upstream run-level "
            "evidence and keeps its own new-source work within the declared cap."
        )
        normalized_tasks.append(normalized_task)
        materializations.append(
            {
                "task_id": normalized_task.get("task_id"),
                "task_index": index,
                "field": "bounded_tasks",
                "materialization": "source_cap_feasibility_reuse_upstream_pool",
                "previous_required_sources": required_sources,
                "max_sources": max_sources,
                "source_cap_reasons": list(source_reasons),
                "explicit_requirement_counts": dict(source_counts),
            }
        )
    normalized["bounded_tasks"] = normalized_tasks
    return normalized, materializations


def _candidate_task_subject_anchor(
    task: Mapping[str, Any],
    *,
    fallback: str,
) -> str:
    for value in (
        task.get("query"),
        task.get("done_condition"),
        fallback,
    ):
        text = str(value or "").strip()
        if not text:
            continue
        tokens = _semantic_release_ordered_meaningful_token_records(text)
        selected = [
            record["display"]
            for record in tokens[:6]
            if str(record.get("display") or "").strip()
        ]
        if selected:
            return " ".join(selected)
    return ""


def _candidate_declared_global_source_budget(constraint: Any) -> int | None:
    if not isinstance(constraint, str):
        return None
    text = str(constraint)
    lowered = text.lower()
    if not (
        "source" in lowered
        or "retrieval" in lowered
        or "max_sources" in lowered
        or "출처" in text
    ):
        return None
    if "per-task" in lowered or "per task" in lowered or "each bounded task" in lowered:
        if not any(
            marker in lowered
            for marker in ("overall", "global", "total", "aggregate", "sum", "budget")
        ):
            return None
    patterns = (
        r"\b(?:overall|global|total|aggregate|cumulative)\s+source\s+budget\s*(?:is|=|:)?\s*(?P<cap>\d{1,4})\b",
        r"\bmax_unique_sources\b\s*(?:=|:|is|must be|must not exceed|not exceed|at most|no more than)\s*(?P<cap>\d{1,4})\b",
        r"\b(?:overall|global|total|aggregate|cumulative)\b[^.\n;]{0,120}\b(?:source|sources|retrievals?|max_sources)\b[^0-9]{0,40}\b(?P<cap>\d{1,4})\b",
        r"\b(?:source|sources|retrievals?|max_sources)\b[^.\n;]{0,120}\b(?:overall|global|total|aggregate|cumulative|budget|cap|limit|sum)\b[^0-9]{0,40}\b(?P<cap>\d{1,4})\b",
        r"\bsum\s*(?:of\s*)?(?:bounded_tasks\.)?max_sources\s*(?:<=|≤|=|is|must not exceed|not exceed|at most|no more than)\s*(?P<cap>\d{1,4})\b",
        r"\b(?:budget|cap|limit|max(?:imum)?|at most|no more than)\b[^.\n;]{0,80}\b(?P<cap>\d{1,4})\b[^.\n;]{0,80}\b(?:source|sources|source retrievals|retrievals?)\b",
        r"\b(?P<cap>\d{1,4})\b\s*(?:source|sources|source retrievals|retrievals?)\b[^.\n;]{0,100}\b(?:budget|cap|limit|max(?:imum)?|overall|total|aggregate|sum)\b",
    )
    budgets: list[int] = []
    for pattern in patterns:
        for match in re.finditer(pattern, lowered):
            try:
                budgets.append(int(match.group("cap")))
            except (TypeError, ValueError):
                continue
    if not budgets:
        return None
    return budgets[0]


def _semantic_task_source_cap_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            numeric = float(value.strip())
        except ValueError:
            return None
        if numeric.is_integer():
            return int(numeric)
    return None


def _materialize_candidate_budget_caps(
    candidate: Mapping[str, Any],
    *,
    budget_cap: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized = copy.deepcopy(dict(candidate))
    if not isinstance(budget_cap, Mapping):
        return normalized, []

    materializations: list[dict[str, Any]] = []
    raw_constraints = normalized.get("constraints")
    max_results = _semantic_task_source_cap_int(budget_cap.get("max_results"))
    if max_results is not None and max_results >= 1 and isinstance(raw_constraints, list):
        if not _candidate_constraints_mention_search_result_cap(
            raw_constraints,
            max_results=max_results,
        ):
            normalized["constraints"] = [
                *raw_constraints,
                f"Search result cap: each bounded task must not exceed max_results={max_results}.",
            ]
            materializations.append(
                {
                    "field": "constraints",
                    "max_results": max_results,
                    "materialization": "appended_search_result_cap_constraint",
                }
            )

    raw_tasks = normalized.get("bounded_tasks")
    if max_results is not None and max_results >= 1 and isinstance(raw_tasks, list):
        normalized_tasks: list[Any] = []
        task_materializations: list[dict[str, Any]] = []
        for index, task in enumerate(raw_tasks, start=1):
            if not isinstance(task, Mapping):
                normalized_tasks.append(task)
                continue
            normalized_task = dict(task)
            previous = _semantic_task_source_cap_int(task.get("max_results"))
            if previous != max_results:
                normalized_task["max_results"] = max_results
                task_materializations.append(
                    {
                        "task_id": task.get("task_id"),
                        "task_index": index,
                        "previous_max_results": previous,
                        "materialized_max_results": max_results,
                    }
                )
            normalized_tasks.append(normalized_task)
        normalized["bounded_tasks"] = normalized_tasks
        if task_materializations:
            materializations.append(
                {
                    "field": "bounded_tasks.max_results",
                    "max_results": max_results,
                    "task_count": len(task_materializations),
                    "tasks": task_materializations,
                }
            )

    max_sources = _semantic_task_source_cap_int(budget_cap.get("max_sources"))
    if max_sources is not None and max_sources > 0:
        normalized, source_budget_materialization = (
            _materialize_candidate_runner_source_budget_cap(
                normalized,
                max_sources=max_sources,
            )
        )
        if source_budget_materialization:
            materializations.append(source_budget_materialization)

    return normalized, materializations


def _materialize_candidate_runner_source_budget_cap(
    candidate: Mapping[str, Any],
    *,
    max_sources: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    normalized = copy.deepcopy(dict(candidate))
    raw_tasks = normalized.get("bounded_tasks")
    if not isinstance(raw_tasks, list):
        return normalized, None
    caps = [
        cap
        for cap in (
            _semantic_task_source_cap_int(task.get("max_sources"))
            for task in raw_tasks
            if isinstance(task, Mapping)
        )
        if cap is not None
    ]
    if not caps:
        return normalized, None

    total_cap = sum(caps)
    existing_runner_budget = normalized.get("runner_source_budget")
    existing_cap = _candidate_runner_source_budget_cap(existing_runner_budget)
    declared_budget = (
        min(existing_cap, max_sources) if existing_cap is not None else max_sources
    )
    runner_source_budget = _candidate_runner_source_budget_record(
        declared_source_budget=declared_budget,
        task_max_sources_sum=total_cap,
        bounded_task_count=len(caps),
        removed_constraints=[],
    )
    if isinstance(existing_runner_budget, Mapping):
        merged_runner_budget = _review_visible_runner_source_budget_metadata(
            existing_runner_budget
        )
        merged_runner_budget.update(runner_source_budget)
        runner_source_budget = merged_runner_budget
    normalized["runner_source_budget"] = runner_source_budget

    constraints = normalized.get("constraints")
    appended_constraint = None
    if isinstance(constraints, list) and not _candidate_constraints_preserve_runner_source_budget(
        constraints,
        declared_source_budget=declared_budget,
    ):
        appended_constraint = (
            "Runner-level source budget is preserved as a unique-source reuse "
            f"budget: max_unique_sources={declared_budget}. "
            f"The combined per-task source ceilings total {total_cap}, so "
            "execution must reuse sources through a shared source pool and must "
            "not treat per-task max_sources as additive permission for unique "
            "source retrievals."
        )
        normalized["constraints"] = [*constraints, appended_constraint]

    materialization = {
        "field": "runner_source_budget",
        "materialization": "preserved_request_source_budget",
        "request_max_sources": max_sources,
        "preserved_declared_source_budget": declared_budget,
        "task_max_sources_sum": total_cap,
        "bounded_task_count": len(caps),
        "reuse_required": total_cap > declared_budget,
        "runner_source_budget": copy.deepcopy(runner_source_budget),
    }
    if appended_constraint is not None:
        materialization["replacement_constraint"] = appended_constraint
    return normalized, materialization


def _candidate_constraints_preserve_runner_source_budget(
    constraints: Sequence[Any],
    *,
    declared_source_budget: int,
) -> bool:
    return any(
        _candidate_declared_global_source_budget(constraint)
        == declared_source_budget
        and _candidate_constraint_preserves_executable_source_budget(
            constraint,
            declared_source_budget=declared_source_budget,
        )
        for constraint in constraints
    )


def _review_visible_runner_source_budget_metadata(
    runner_source_budget: Any,
) -> dict[str, Any]:
    if not isinstance(runner_source_budget, Mapping):
        return {}
    renamed_fields = {
        "materialized_from_budget_cap": "materialized_from_request_source_limit",
        "budget_cap_max_sources": "request_max_sources",
    }
    visible: dict[str, Any] = {}
    for raw_key, value in runner_source_budget.items():
        key = renamed_fields.get(str(raw_key), str(raw_key))
        if _contains_budget_cap_term(key):
            continue
        if _contains_budget_cap_term(value):
            continue
        visible[key] = value
    return visible


def _contains_budget_cap_term(value: Any) -> bool:
    normalized = _normalize_text(json.dumps(value, ensure_ascii=False, default=str))
    return _contains_normalized_phrase(normalized, "budget cap")


def _candidate_constraints_mention_search_result_cap(
    constraints: Sequence[Any],
    *,
    max_results: int,
) -> bool:
    text = json.dumps(list(constraints), ensure_ascii=False).lower()
    patterns = (
        rf"\bmax_results\b\s*[:=]\s*{max_results}\b",
        rf"\bmax[_ -]?results\b[^0-9]{{0,40}}\b{max_results}\b",
        rf"\bsearch results\b[^0-9]{{0,80}}\b{max_results}\b",
        rf"\b검색 결과\b[^0-9]{{0,80}}\b{max_results}\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _materialize_candidate_expected_evidence(
    candidate: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized = copy.deepcopy(dict(candidate))
    raw_tasks = normalized.get("bounded_tasks")
    if not isinstance(raw_tasks, list):
        return normalized, []

    raw_angles = normalized.get("angles")
    angle_records = raw_angles if isinstance(raw_angles, list) else []
    angles_by_id = {
        str(angle.get("angle_id") or ""): angle
        for angle in angle_records
        if isinstance(angle, Mapping)
    }
    normalized_tasks: list[Any] = []
    materializations: list[dict[str, Any]] = []
    for index, task in enumerate(raw_tasks, start=1):
        if not isinstance(task, Mapping):
            normalized_tasks.append(task)
            continue
        normalized_task = dict(task)
        current = _string_list(normalized_task.get("expected_evidence"))
        if current:
            normalized_tasks.append(normalized_task)
            continue
        angle = angles_by_id.get(str(normalized_task.get("angle_id") or ""))
        inferred = _candidate_task_expected_evidence_from_context(
            normalized_task,
            angle=angle if isinstance(angle, Mapping) else {},
        )
        if not inferred:
            normalized_tasks.append(normalized_task)
            continue
        normalized_task["expected_evidence"] = inferred
        normalized_tasks.append(normalized_task)
        materializations.append(
            {
                "task_id": normalized_task.get("task_id"),
                "task_index": index,
                "angle_id": normalized_task.get("angle_id"),
                "field": "bounded_tasks.expected_evidence",
                "materialized_expected_evidence": inferred,
                "materialization": "inferred_expected_evidence_from_visual_context",
            }
        )
    normalized["bounded_tasks"] = normalized_tasks
    return normalized, materializations


def _materialize_candidate_placeholder_selection_workflow(
    candidate: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized = copy.deepcopy(dict(candidate))
    angles = [
        angle
        for angle in normalized.get("angles") or []
        if isinstance(angle, Mapping)
    ]
    tasks = [
        task
        for task in normalized.get("bounded_tasks") or []
        if isinstance(task, Mapping)
    ]
    constraints = normalized.get("constraints")
    constraint_records = constraints if isinstance(constraints, list) else []
    labels = _candidate_placeholder_jurisdiction_labels(
        angles=angles,
        tasks=tasks,
        constraints=constraint_records,
    )
    if not labels:
        return normalized, []
    has_selection_workflow = _candidate_has_placeholder_selection_workflow(
        angles=angles,
        tasks=tasks,
        constraints=constraint_records,
    )
    existing_unbound_labels = _candidate_unbound_placeholder_jurisdiction_labels(
        plan=normalized,
        placeholder_labels=labels,
    )
    if not existing_unbound_labels:
        return normalized, []
    materialized_bindings = _candidate_materialized_placeholder_bindings(
        normalized,
        placeholder_labels=existing_unbound_labels,
    )
    if materialized_bindings:
        existing_binding = normalized.get("placeholder_binding")
        merged_binding = (
            dict(existing_binding) if isinstance(existing_binding, Mapping) else {}
        )
        merged_binding.update(materialized_bindings)
        normalized["placeholder_binding"] = merged_binding

    selection_constraint = (
        "Placeholder jurisdiction labels must be bound before evidence collection: "
        f"select and name the jurisdictions represented by {', '.join(labels[:6])} "
        "using explicit selection criteria, then map every placeholder-dependent "
        "claim back to the selected named jurisdiction."
    )
    if isinstance(constraints, list):
        normalized_constraints = list(constraints)
        if not has_selection_workflow:
            normalized_constraints.append(selection_constraint)
        if materialized_bindings:
            normalized_constraints.append(
                {
                    "constraint_type": "placeholder_binding",
                    "placeholder_binding": copy.deepcopy(materialized_bindings),
                    "selection_basis": "deterministic_question_context",
                    "message": (
                        "Use these concrete named jurisdictions whenever placeholder "
                        "jurisdiction labels appear in tasks, angles, constraints, or claims."
                    ),
                }
            )
        normalized["constraints"] = normalized_constraints

    raw_tasks = normalized.get("bounded_tasks")
    task_materialization: dict[str, Any] | None = None
    if isinstance(raw_tasks, list):
        normalized_tasks: list[Any] = []
        task_updated = False
        for index, task in enumerate(raw_tasks, start=1):
            if not isinstance(task, Mapping):
                normalized_tasks.append(task)
                continue
            normalized_task = dict(task)
            record_bindings = _candidate_record_placeholder_bindings(
                task,
                placeholder_binding=materialized_bindings,
            )
            if record_bindings:
                normalized_task["placeholder_binding"] = record_bindings
            if not task_updated and record_bindings:
                criteria = _string_list(normalized_task.get("success_criteria"))
                normalized_task["success_criteria"] = [
                    *criteria,
                    (
                        "Before comparing evidence, run a jurisdiction selection "
                        "workflow that binds each placeholder municipality to a named "
                        "jurisdiction with explicit selection criteria."
                    ),
                ]
                done_condition = str(normalized_task.get("done_condition") or "")
                if "selection workflow" not in done_condition.lower():
                    normalized_task["done_condition"] = (
                        f"{done_condition.rstrip()} First bind placeholder "
                        "jurisdictions to named municipalities through the selection "
                        "workflow before recording source-backed findings."
                    ).strip()
                task_materialization = {
                    "task_id": normalized_task.get("task_id"),
                    "task_index": index,
                    "field": "bounded_tasks.success_criteria",
                    "materialization": "added_placeholder_jurisdiction_selection_workflow",
                    "placeholder_binding": copy.deepcopy(record_bindings),
                }
                task_updated = True
            normalized_tasks.append(normalized_task)
        normalized["bounded_tasks"] = normalized_tasks

    raw_angles = normalized.get("angles")
    angle_materialization: dict[str, Any] | None = None
    if isinstance(raw_angles, list):
        normalized_angles: list[Any] = []
        angle_updated = False
        for index, angle in enumerate(raw_angles, start=1):
            if not isinstance(angle, Mapping):
                normalized_angles.append(angle)
                continue
            normalized_angle = dict(angle)
            record_bindings = _candidate_record_placeholder_bindings(
                angle,
                placeholder_binding=materialized_bindings,
            )
            if record_bindings:
                normalized_angle["placeholder_binding"] = record_bindings
            if not angle_updated and record_bindings:
                checks = _string_list(normalized_angle.get("risk_or_contradiction_checks"))
                normalized_angle["risk_or_contradiction_checks"] = [
                    *checks,
                    (
                        "Verify that the jurisdiction selection workflow resolves each "
                        "placeholder municipality to a named jurisdiction before synthesis."
                    ),
                ]
                angle_materialization = {
                    "angle_id": normalized_angle.get("angle_id"),
                    "angle_index": index,
                    "field": "angles.risk_or_contradiction_checks",
                    "materialization": "added_placeholder_jurisdiction_selection_check",
                    "placeholder_binding": copy.deepcopy(record_bindings),
                }
                angle_updated = True
            normalized_angles.append(normalized_angle)
        normalized["angles"] = normalized_angles

    materialization = {
        "field": "placeholder_jurisdiction_workflow",
        "materialization": "bound_placeholder_jurisdictions",
        "placeholder_labels": labels,
        "unbound_placeholder_labels": existing_unbound_labels,
        "placeholder_binding": copy.deepcopy(materialized_bindings),
        "selection_constraint": selection_constraint,
    }
    if not materialized_bindings:
        materialization["binding_status"] = "unresolved_no_question_context"
    else:
        materialization["binding_status"] = "bound_from_question_context"
    if task_materialization:
        materialization["task_materialization"] = task_materialization
    if angle_materialization:
        materialization["angle_materialization"] = angle_materialization
    return normalized, [materialization]


def _materialize_candidate_visual_image_cap_feasibility(
    candidate: Mapping[str, Any],
    *,
    budget_cap: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized = copy.deepcopy(dict(candidate))
    raw_tasks = normalized.get("bounded_tasks")
    if not isinstance(raw_tasks, list):
        return normalized, []

    run_image_budget = (
        _semantic_task_source_cap_int(budget_cap.get("max_images"))
        if isinstance(budget_cap, Mapping)
        else None
    )
    schema_image_limit = 3
    allowed_image_cap = schema_image_limit
    if run_image_budget is not None:
        allowed_image_cap = max(0, min(schema_image_limit, run_image_budget))
    normalized_tasks: list[Any] = []
    materializations: list[dict[str, Any]] = []
    for index, task in enumerate(raw_tasks, start=1):
        if not isinstance(task, Mapping):
            normalized_tasks.append(task)
            continue
        normalized_task = dict(task)
        max_images = _semantic_task_source_cap_int(task.get("max_images")) or 0
        if str(task.get("route") or "") == "text_only" or max_images <= 0:
            normalized_tasks.append(normalized_task)
            continue
        task_materializations: list[dict[str, Any]] = []
        required_images, image_counts = _candidate_task_required_image_count(task)
        if required_images > max_images and allowed_image_cap > max_images:
            repaired_max_images = min(required_images, allowed_image_cap)
            if repaired_max_images > max_images:
                normalized_task["max_images"] = repaired_max_images
                task_materializations.append(
                    {
                        "field": "bounded_tasks.max_images",
                        "previous": max_images,
                        "materialized": repaired_max_images,
                        "required_images": required_images,
                        "explicit_requirement_counts": image_counts,
                        "materialization": "raised_visual_image_cap_within_budget",
                    }
                )
                max_images = repaired_max_images
        if task_materializations:
            materializations.append(
                {
                    "task_id": normalized_task.get("task_id"),
                    "task_index": index,
                    "max_images": max_images,
                    "materialization": "capped_visual_image_demands_to_task_budget",
                    "fields": task_materializations,
                }
            )
        normalized_tasks.append(normalized_task)
    normalized["bounded_tasks"] = normalized_tasks
    return normalized, materializations


def _semantic_count_word_to_int(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    return {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "twenty": 20,
    }.get(value.lower())


def _candidate_task_expected_evidence_from_context(
    task: Mapping[str, Any],
    *,
    angle: Mapping[str, Any],
) -> list[str]:
    evidence: list[str] = []
    task_route = str(task.get("route") or "")
    if task_route == "text_only":
        return evidence
    task_need = str(task.get("evidence_need") or "")
    angle_need = str(angle.get("evidence_need") or "")
    route = str(task_route or angle.get("route") or "text_only")
    task_targets = _string_list(task.get("expected_visual_targets"))
    angle_targets = _string_list(angle.get("expected_visual_targets"))
    targets = [*task_targets, *angle_targets]
    text = json.dumps(
        {
            "task_evidence_need": task_need,
            "angle_evidence_need": angle_need,
            "route": route,
            "expected_visual_targets": targets,
            "expected_artifacts": [
                *_string_list(task.get("expected_artifacts")),
                *_string_list(angle.get("expected_artifacts")),
            ],
            "query": task.get("query"),
            "success_criteria": task.get("success_criteria"),
            "done_condition": task.get("done_condition"),
        },
        ensure_ascii=False,
        sort_keys=True,
    ).lower()

    for need in (task_need, angle_need):
        if need in VISUAL_EXPECTED_EVIDENCE:
            evidence.append(need)

    visual_obligation = (
        route != "text_only"
        or bool(targets)
        or _semantic_task_expected_visual_artifacts(task)
        or _semantic_task_expected_visual_artifacts(angle)
    )
    if not visual_obligation:
        return _ordered_unique(evidence)

    if _candidate_visual_context_needs_examples(text, targets) or not evidence:
        evidence.append("visual_example")
    if _candidate_visual_context_needs_observation(text, targets) or not evidence:
        evidence.append("visual_observation")
    if _candidate_visual_context_needs_vlm_analysis(text):
        evidence.append("vlm_analysis")
    return _ordered_unique(evidence)


def _candidate_visual_context_needs_examples(
    text: str,
    targets: Sequence[str],
) -> bool:
    target_text = " ".join(str(target) for target in targets).lower()
    return _contains_any(
        f"{text} {target_text}",
        (
            "example",
            "examples",
            "representative",
            "candidate",
            "candidates",
            "gallery",
            "image set",
            "source images",
            "poster",
            "public examples",
            "\uc608\uc2dc",
            "\uc0ac\ub840",
            "\ud3ec\uc2a4\ud130",
            "\uc774\ubbf8\uc9c0",
            "\uc0ac\uc9c4",
        ),
    )


def _candidate_visual_context_needs_observation(
    text: str,
    targets: Sequence[str],
) -> bool:
    target_text = " ".join(str(target) for target in targets).lower()
    return _contains_any(
        f"{text} {target_text}",
        (
            "observation",
            "observe",
            "visible",
            "inspect",
            "inspection",
            "analysis",
            "analyze",
            "interpret",
            "feature",
            "structure",
            "chart",
            "diagram",
            "figure",
            "comparison",
            "compare",
            "\ud310\ub3c5",
            "\ubd84\uc11d",
            "\uad6c\uc870",
            "\ud2b9\uc9d5",
            "\ucc28\ud2b8",
            "\uadf8\ub9bc",
            "\ube44\uad50",
        ),
    )


def _candidate_visual_context_needs_vlm_analysis(text: str) -> bool:
    return _contains_any(
        text,
        (
            "vlm",
            "vision model",
            "image interpretation",
            "visual interpretation",
            "chart reading",
            "figure reading",
            "\ud310\ub3c5",
            "\uc2dc\uac01 \ubd84\uc11d",
        ),
    )


def _materialize_candidate_angle_title_prompt_anchors(
    candidate: Mapping[str, Any],
    *,
    original_question: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Add prompt-specific anchors to shallow angle titles before validation.

    This preserves the strict validator: the repaired candidate must still pass
    the same release-depth checks. The materialization only edits a short title,
    never the research question, route, scope, source policy, or bounded tasks.
    """

    normalized = copy.deepcopy(dict(candidate))
    prompt_anchor_records = _semantic_release_ordered_meaningful_token_records(
        original_question
    )
    prompt_tokens = [record["token"] for record in prompt_anchor_records]
    if len(prompt_tokens) < SEMANTIC_RELEASE_MIN_ANGLE_OVERLAP_TOKENS:
        return normalized, []
    raw_angles = normalized.get("angles")
    if not isinstance(raw_angles, list):
        return normalized, []

    repairable_count = 0
    mapping_angle_count = 0
    for angle in raw_angles:
        if not isinstance(angle, Mapping):
            continue
        mapping_angle_count += 1
        title = str(angle.get("title") or "").strip()
        research_question = str(angle.get("research_question") or "").strip()
        if not title or not research_question:
            continue
        if _semantic_release_generic_or_original_text(title, original_question):
            continue
        combined_text = f"{title} {research_question}".strip()
        if _semantic_release_placeholder_text(combined_text):
            continue
        overlap_tokens = (
            set(_semantic_release_meaningful_token_list(combined_text))
            & set(prompt_tokens)
        )
        if len(overlap_tokens) < SEMANTIC_RELEASE_MIN_ANGLE_OVERLAP_TOKENS:
            repairable_count += 1
    if repairable_count > max(2, math.floor(mapping_angle_count * 0.4)):
        return normalized, []

    materializations: list[dict[str, Any]] = []
    normalized_angles: list[Any] = []
    for index, angle in enumerate(raw_angles, start=1):
        if not isinstance(angle, Mapping):
            normalized_angles.append(angle)
            continue
        normalized_angle = dict(angle)
        title = str(normalized_angle.get("title") or "").strip()
        research_question = str(normalized_angle.get("research_question") or "").strip()
        if not title or not research_question:
            normalized_angles.append(normalized_angle)
            continue
        if _semantic_release_generic_or_original_text(title, original_question):
            normalized_angles.append(normalized_angle)
            continue
        combined_text = f"{title} {research_question}".strip()
        if _semantic_release_placeholder_text(combined_text):
            normalized_angles.append(normalized_angle)
            continue
        overlap_tokens = (
            set(_semantic_release_meaningful_token_list(combined_text))
            & set(prompt_tokens)
        )
        if len(overlap_tokens) >= SEMANTIC_RELEASE_MIN_ANGLE_OVERLAP_TOKENS:
            normalized_angles.append(normalized_angle)
            continue

        anchor_tokens = _semantic_release_angle_title_anchor_tokens(
            prompt_tokens=prompt_tokens,
            existing_overlap=overlap_tokens,
        )
        if len(set(anchor_tokens) | overlap_tokens) < SEMANTIC_RELEASE_MIN_ANGLE_OVERLAP_TOKENS:
            normalized_angles.append(normalized_angle)
            continue
        anchor_display_tokens = _semantic_release_anchor_display_tokens(
            prompt_anchor_records=prompt_anchor_records,
            anchor_tokens=anchor_tokens,
        )
        anchor_phrase = " ".join(anchor_display_tokens)
        repaired_title = f"{anchor_phrase}: {title}"
        if repaired_title == title:
            normalized_angles.append(normalized_angle)
            continue
        normalized_angle["title"] = repaired_title
        materializations.append(
            {
                "angle_id": normalized_angle.get("angle_id"),
                "angle_index": index,
                "field": "angles.title",
                "previous_title": title,
                "materialized_title": repaired_title,
                "materialization": "prepended_prompt_anchor_tokens",
                "previous_meaningful_overlap_count": len(overlap_tokens),
                "minimum_meaningful_overlap": (
                    SEMANTIC_RELEASE_MIN_ANGLE_OVERLAP_TOKENS
                ),
                "anchor_tokens": anchor_tokens,
                "anchor_display_tokens": anchor_display_tokens,
            }
        )
        normalized_angles.append(normalized_angle)
    normalized["angles"] = normalized_angles
    return normalized, materializations


def _repair_candidate_requirement_coverage(
    candidate: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized = copy.deepcopy(dict(candidate))
    raw_coverage = normalized.get("requirement_coverage_map")
    raw_tasks = normalized.get("bounded_tasks")
    if not isinstance(raw_coverage, list) or not isinstance(raw_tasks, list):
        return normalized, []
    tasks = [task for task in raw_tasks if isinstance(task, Mapping)]
    if not tasks:
        return normalized, []

    task_records = [
        {
            "task": task,
            "task_id": str(task.get("task_id") or ""),
            "angle_id": str(task.get("angle_id") or ""),
            "tokens": _semantic_coverage_tokens(_semantic_coverage_task_text(task)),
            "visual": _semantic_coverage_task_is_visual(task),
        }
        for task in tasks
    ]
    materializations: list[dict[str, Any]] = []
    normalized_coverage: list[Any] = []
    for index, coverage in enumerate(raw_coverage, start=1):
        if not isinstance(coverage, Mapping):
            normalized_coverage.append(coverage)
            continue
        repaired = dict(coverage)
        requirement_text = _semantic_coverage_requirement_text(repaired)
        requirement_tokens = _semantic_coverage_tokens(requirement_text)
        if not requirement_tokens:
            normalized_coverage.append(repaired)
            continue
        requirement_visual = _semantic_coverage_requirement_is_visual(
            repaired,
            requirement_text,
        )
        covered_task_ids = _string_list(repaired.get("covered_by_task_ids"))
        covered_angle_ids = _string_list(repaired.get("covered_by_angle_ids"))
        added_task_ids: list[str] = []
        added_angle_ids: list[str] = []
        for record in task_records:
            task_id = record["task_id"]
            if not task_id or task_id in covered_task_ids:
                continue
            overlap = requirement_tokens & record["tokens"]
            if not _semantic_coverage_overlap_supports_task(
                overlap=overlap,
                requirement_visual=requirement_visual,
                task_visual=bool(record["visual"]),
            ):
                continue
            covered_task_ids.append(task_id)
            added_task_ids.append(task_id)
            angle_id = record["angle_id"]
            if angle_id and angle_id not in covered_angle_ids:
                covered_angle_ids.append(angle_id)
                added_angle_ids.append(angle_id)
        if added_task_ids:
            repaired["covered_by_task_ids"] = covered_task_ids
            repaired["covered_by_angle_ids"] = covered_angle_ids
            repaired["coverage_status"] = repaired.get("coverage_status") or "covered"
            materializations.append(
                {
                    "requirement_id": repaired.get("requirement_id")
                    or f"req_{index:03d}",
                    "field": "requirement_coverage_map.covered_by_task_ids",
                    "materialization": "added_semantically_overlapping_tasks",
                    "added_task_ids": added_task_ids,
                    "added_angle_ids": added_angle_ids,
                }
            )
        normalized_coverage.append(repaired)
    normalized["requirement_coverage_map"] = normalized_coverage
    return normalized, materializations


def _semantic_coverage_requirement_text(requirement: Mapping[str, Any]) -> str:
    return json.dumps(
        [
            requirement.get("prompt_text"),
            requirement.get("requirement_text"),
            requirement.get("requirement_type"),
        ],
        ensure_ascii=False,
        sort_keys=True,
    )


def _semantic_coverage_task_text(task: Mapping[str, Any]) -> str:
    return json.dumps(
        [
            task.get("query"),
            task.get("route"),
            task.get("expected_source_types"),
            task.get("expected_visual_targets"),
            task.get("expected_artifacts"),
            task.get("success_criteria"),
            task.get("done_condition"),
            task.get("source_policy"),
        ],
        ensure_ascii=False,
        sort_keys=True,
    )


def _semantic_coverage_tokens(text: str) -> set[str]:
    return set(_semantic_release_meaningful_token_list(text)) - {
        "any",
        "covered",
        "task",
        "tasks",
        "angle",
        "angles",
        "requirement",
        "requirements",
        "source",
        "sources",
        "근거",
        "공식",
        "자료",
        "항목",
        "기준",
        "비교",
    }


def _semantic_coverage_requirement_is_visual(
    requirement: Mapping[str, Any],
    requirement_text: str,
) -> bool:
    requirement_type = str(requirement.get("requirement_type") or "").lower()
    lowered = requirement_text.lower()
    return (
        "visual" in requirement_type
        or "modality" in requirement_type
        or any(
            token in lowered
            for token in (
                "visual",
                "image",
                "model",
                "drawing",
                "diagram",
                "screenshot",
                "시각",
                "이미지",
                "모델",
                "도면",
                "정합성",
            )
        )
    )


def _semantic_coverage_task_is_visual(task: Mapping[str, Any]) -> bool:
    if str(task.get("route") or "") != "text_only":
        return True
    if _string_list(task.get("expected_visual_targets")):
        return True
    max_images = _semantic_task_source_cap_int(task.get("max_images"))
    if max_images is not None and max_images > 0:
        return True
    return False


def _semantic_coverage_overlap_supports_task(
    *,
    overlap: set[str],
    requirement_visual: bool,
    task_visual: bool,
) -> bool:
    if len(overlap) >= 3:
        return True
    if requirement_visual and task_visual and len(overlap) >= 2:
        return True
    return False


def _semantic_release_ordered_meaningful_token_records(text: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_token in re.findall(r"[A-Za-z0-9\uac00-\ud7a3]+", text.lower()):
        if _semantic_release_token_has_hangul(raw_token):
            token = _normalize_korean_semantic_token(raw_token)
            if len(token) < 2:
                continue
        elif len(raw_token) > 2:
            token = raw_token
        else:
            continue
        if token in SEMANTIC_RELEASE_GENERIC_TOKENS:
            continue
        if token in seen:
            continue
        seen.add(token)
        records.append({"token": token, "display": raw_token})
    return records


def _semantic_release_anchor_display_tokens(
    *,
    prompt_anchor_records: Sequence[Mapping[str, str]],
    anchor_tokens: Sequence[str],
) -> list[str]:
    display_by_token = {
        str(record.get("token")): str(record.get("display"))
        for record in prompt_anchor_records
    }
    return [display_by_token.get(token, token) for token in anchor_tokens]


def _semantic_release_angle_title_anchor_tokens(
    *,
    prompt_tokens: Sequence[str],
    existing_overlap: set[str],
) -> list[str]:
    anchors: list[str] = []
    for token in prompt_tokens:
        if token in anchors:
            continue
        anchors.append(token)
        if len(set(anchors) | existing_overlap) >= max(
            SEMANTIC_RELEASE_MIN_ANGLE_OVERLAP_TOKENS,
            min(3, len(prompt_tokens)),
        ):
            break
    return anchors


def _candidate_task_min_executable_sources(
    task: Mapping[str, Any],
) -> tuple[int, list[str]]:
    text = _candidate_task_source_cap_text(task)
    source_family_count = _candidate_task_declared_source_family_count(task)
    named_source_entity_count = _candidate_task_named_source_entity_count(task)
    explicit_counts = _candidate_task_explicit_requirement_counts(task)
    reasons = [
        label
        for label, needles in SEMANTIC_MULTI_SOURCE_CAP_NEEDLES.items()
        if _semantic_source_cap_text_has_any(text, needles)
    ]
    if source_family_count >= 2:
        reasons.append("multiple_declared_source_families")
    if named_source_entity_count >= 2:
        reasons.append("multiple_named_source_entities")
    for kind in ("source", "vendor", "jurisdiction", "source_artifact"):
        count = int(explicit_counts.get(kind) or 0)
        if count >= 2:
            reasons.append(f"explicit_{kind}_count")
    reason_set = set(reasons)
    minimum = SEMANTIC_TASK_MIN_SOURCES
    if reason_set & {"comparison", "freshness", "contradiction"}:
        minimum = max(minimum, 2)
    if "official_record" in reason_set and (
        "multiple_declared_source_families" in reason_set
        or reason_set & {"comparison", "freshness", "contradiction"}
    ):
        minimum = max(minimum, 2)
    if source_family_count >= 2 and reason_set:
        minimum = max(minimum, source_family_count)
    if named_source_entity_count >= 2 and (
        "official_record" in reason_set
        or reason_set & {"comparison", "freshness", "contradiction"}
    ):
        minimum = max(minimum, named_source_entity_count)
    for kind in ("source", "vendor", "jurisdiction", "source_artifact"):
        minimum = max(minimum, int(explicit_counts.get(kind) or 0))
    return min(SEMANTIC_TASK_MAX_SOURCES, minimum), list(dict.fromkeys(reasons))


def _candidate_task_required_source_count(
    task: Mapping[str, Any],
) -> tuple[int, list[str], dict[str, int]]:
    capped_minimum, reasons = _candidate_task_min_executable_sources(task)
    explicit_counts = _candidate_task_explicit_requirement_counts(task)
    required = capped_minimum
    for kind in ("source", "vendor", "jurisdiction", "source_artifact"):
        required = max(required, int(explicit_counts.get(kind) or 0))
    return required, reasons, explicit_counts


def _candidate_task_required_image_count(task: Mapping[str, Any]) -> tuple[int, dict[str, int]]:
    counts = _candidate_task_explicit_requirement_counts(task)
    per_entity_count, per_entity_details = _candidate_task_per_entity_image_requirement_count(
        task
    )
    counts.update(per_entity_details)
    required = max(int(counts.get("image") or 0), per_entity_count)
    return required, counts


def _candidate_task_cap_feasibility_failures(
    tasks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for task in tasks:
        task_id = task.get("task_id")
        max_sources = _semantic_task_source_cap_int(task.get("max_sources"))
        required_sources, source_reasons, source_counts = (
            _candidate_task_required_source_count(task)
        )
        if max_sources is not None and required_sources > max_sources:
            dominant_kind = _dominant_cap_requirement_kind(
                source_counts,
                fallback="source",
                source_reasons=source_reasons,
            )
            failures.append(
                {
                    "code": "bounded_task_requirement_exceeds_max_sources",
                    "task_id": task_id,
                    "requirement_kind": dominant_kind,
                    "required_count": required_sources,
                    "max_sources": max_sources,
                    "explicit_requirement_counts": source_counts,
                    "source_cap_reasons": source_reasons,
                    "message": (
                        "Bounded task requires more distinct source/vendor/"
                        "jurisdiction/source-artifact evidence than max_sources permits."
                    ),
                }
            )
        max_images = _semantic_task_source_cap_int(task.get("max_images"))
        required_images, image_counts = _candidate_task_required_image_count(task)
        if max_images is not None and required_images > max_images:
            failures.append(
                {
                    "code": "bounded_task_requirement_exceeds_max_images",
                    "task_id": task_id,
                    "requirement_kind": "image",
                    "required_count": required_images,
                    "max_images": max_images,
                    "explicit_requirement_counts": image_counts,
                    "message": (
                        "Bounded task done condition or visual fields require more "
                        "images than max_images permits."
                    ),
                }
            )
    return failures


def _dominant_cap_requirement_kind(
    counts: Mapping[str, int],
    *,
    fallback: str,
    source_reasons: Sequence[str],
) -> str:
    explicit = [
        (kind, int(value))
        for kind, value in counts.items()
        if kind != "image" and int(value or 0) > 0
    ]
    if explicit:
        explicit.sort(key=lambda item: item[1], reverse=True)
        return explicit[0][0]
    for reason in source_reasons:
        if reason.startswith("explicit_") and reason.endswith("_count"):
            return reason.removeprefix("explicit_").removesuffix("_count")
        if reason == "multiple_named_source_entities":
            return "vendor"
        if reason == "multiple_declared_source_families":
            return "source"
    return fallback


def _candidate_task_explicit_requirement_counts(task: Mapping[str, Any]) -> dict[str, int]:
    text_by_kind = {
        "source": _candidate_task_requirement_count_text(task, "source"),
        "vendor": _candidate_task_requirement_count_text(task, "vendor"),
        "jurisdiction": _candidate_task_requirement_count_text(task, "jurisdiction"),
        "source_artifact": _candidate_task_requirement_count_text(task, "source_artifact"),
        "image": _candidate_task_requirement_count_text(task, "image"),
    }
    return {
        kind: _max_explicit_requirement_count(text, kind=kind)
        for kind, text in text_by_kind.items()
    }


def _candidate_task_per_entity_image_requirement_count(
    task: Mapping[str, Any],
) -> tuple[int, dict[str, int]]:
    text = _candidate_task_requirement_count_text(task, "image")
    lowered = str(text or "").lower()
    if not lowered:
        return 0, {}

    image_nouns = (
        "image",
        "photo",
        "screenshot",
        "figure",
        "diagram",
        "chart",
        "poster",
        "visual example",
    )
    entity_nouns = (
        ("program", "programs"),
        ("vendor", "vendors"),
        ("provider", "providers"),
        ("product", "products"),
        ("service", "services"),
        ("jurisdiction", "jurisdictions"),
        ("municipality", "municipalities"),
        ("city", "cities"),
        ("county", "counties"),
        ("region", "regions"),
        ("country", "countries"),
        ("agency", "agencies"),
        ("source", "sources"),
        ("model", "models"),
        ("case", "cases"),
        ("location", "locations"),
        ("site", "sites"),
    )
    count_pattern = (
        r"(?P<count>(?:[1-9]\d*)|one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve|thirteen|fourteen|fifteen|twenty)"
    )
    image_noun_pattern = "|".join(
        re.escape(noun) for noun in sorted(image_nouns, key=len, reverse=True)
    )
    per_requirements: list[tuple[str, int]] = []
    for singular, plural in entity_nouns:
        per_entity_pattern = (
            rf"(?:{count_pattern}\s+)?"
            rf"(?:distinct\s+|different\s+|representative\s+|official\s+|primary\s+)?"
            rf"(?:{image_noun_pattern})\b[\w\s-]{{0,80}}\bper\s+{re.escape(singular)}\b"
        )
        if not re.search(per_entity_pattern, lowered):
            continue
        per_match = re.search(per_entity_pattern, lowered)
        per_image_count = 1
        if per_match and per_match.groupdict().get("count"):
            parsed = _semantic_count_word_to_int(per_match.group("count"))
            if parsed is not None:
                per_image_count = parsed
        entity_count = _max_entity_count_for_image_per_entity_text(
            lowered,
            singular=singular,
            plural=plural,
        )
        if entity_count <= 0:
            continue
        per_requirements.append((singular, per_image_count * entity_count))

    if not per_requirements:
        return 0, {}
    dominant_entity, required_count = max(per_requirements, key=lambda item: item[1])
    return required_count, {
        "image_per_entity": required_count,
        "image_per_entity_count": required_count,
        f"image_per_{dominant_entity}": required_count,
    }


def _max_entity_count_for_image_per_entity_text(
    text: str,
    *,
    singular: str,
    plural: str,
) -> int:
    count_pattern = (
        r"(?P<count>(?:[1-9]\d*)|one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve|thirteen|fourteen|fifteen|twenty)"
    )
    patterns = (
        rf"\b(?:across|from|cover(?:ing)?|include|including|for|among|between|sampled from)\s+"
        rf"{count_pattern}\s+(?:distinct\s+|different\s+|sampled\s+)?{re.escape(plural)}\b",
        rf"\b{count_pattern}\s+(?:distinct\s+|different\s+|sampled\s+)?{re.escape(plural)}\b",
        rf"\b(?:each|every)\s+of\s+{count_pattern}\s+"
        rf"(?:distinct\s+|different\s+|sampled\s+)?{re.escape(plural)}\b",
    )
    counts: list[int] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            parsed = _semantic_count_word_to_int(match.group("count"))
            if parsed is not None:
                counts.append(parsed)
    if counts:
        return max(counts)
    return 0


def _candidate_task_requirement_count_text(
    task: Mapping[str, Any],
    kind: str,
) -> str:
    if kind == "image":
        fields = (
            "query",
            "expected_visual_targets",
            "expected_artifacts",
            "success_criteria",
            "done_condition",
        )
    elif kind == "source_artifact":
        fields = (
            "expected_artifacts",
            "success_criteria",
            "done_condition",
        )
    else:
        fields = SEMANTIC_MULTI_SOURCE_CAP_FIELDS
    return json.dumps([task.get(field) for field in fields], ensure_ascii=False, sort_keys=True)


def _max_explicit_requirement_count(text: str, *, kind: str) -> int:
    lowered = str(text or "").lower()
    nouns = {
        "source": (
            "source",
            "sources",
            "official source",
            "official sources",
            "primary source",
            "primary sources",
            "regulatory source",
            "regulatory sources",
            "source record",
            "source records",
        ),
        "vendor": (
            "vendor",
            "vendors",
            "provider",
            "providers",
            "browser",
            "browsers",
            "product",
            "products",
        ),
        "jurisdiction": (
            "jurisdiction",
            "jurisdictions",
            "municipality",
            "municipalities",
            "city",
            "cities",
            "county",
            "counties",
            "region",
            "regions",
            "country",
            "countries",
        ),
        "source_artifact": (
            "source-backed artifact",
            "source-backed artifacts",
            "official record",
            "official records",
            "regulatory record",
            "regulatory records",
            "source excerpt",
            "source excerpts",
        ),
        "image": (
            "image",
            "images",
            "photo",
            "photos",
            "screenshot",
            "screenshots",
            "figure",
            "figures",
            "diagram",
            "diagrams",
            "chart",
            "charts",
            "poster",
            "posters",
            "visual example",
            "visual examples",
        ),
    }[kind]
    noun_pattern = "|".join(re.escape(noun) for noun in sorted(nouns, key=len, reverse=True))
    digit_count_pattern = r"(?<![-_A-Za-z0-9])(?:[1-9]\d*)"
    count_pattern = (
        rf"{digit_count_pattern}|one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve|thirteen|fourteen|fifteen|twenty"
    )
    patterns = (
        rf"\b(?:at least|minimum of|no fewer than|compare|across|from|cover|collect|inspect|analyze|review|use|include)\s+"
        rf"(?P<count>{count_pattern})\s+(?:distinct\s+|different\s+|representative\s+|official\s+|primary\s+|regulatory\s+)?(?:{noun_pattern})\b",
        rf"\b(?P<count>{count_pattern})\s+(?:distinct\s+|different\s+|representative\s+|official\s+|primary\s+|regulatory\s+)?(?:{noun_pattern})\b",
    )
    counts: list[int] = []
    for pattern in patterns:
        for match in re.finditer(pattern, lowered):
            count = _semantic_count_word_to_int(match.group("count"))
            if count is not None:
                counts.append(count)
    return max(counts or [0])


def _candidate_task_source_cap_text(task: Mapping[str, Any]) -> str:
    values = [task.get(field_name) for field_name in SEMANTIC_MULTI_SOURCE_CAP_FIELDS]
    return json.dumps(values, ensure_ascii=False, sort_keys=True).lower()


def _candidate_task_declared_source_family_count(task: Mapping[str, Any]) -> int:
    groups = [_string_list(task.get("expected_source_types"))]
    source_policy = task.get("source_policy")
    if isinstance(source_policy, Mapping):
        for key in SEMANTIC_SOURCE_FAMILY_POLICY_KEYS:
            groups.append(_string_list(source_policy.get(key)))
    counts = []
    for values in groups:
        normalized = {
            _normalize_text(value)
            for value in values
            if _normalize_text(value)
        }
        counts.append(len(normalized))
    return max(counts or [0])


def _candidate_task_named_source_entity_count(task: Mapping[str, Any]) -> int:
    raw_text = json.dumps(
        [task.get(field_name) for field_name in SEMANTIC_MULTI_SOURCE_CAP_FIELDS],
        ensure_ascii=False,
        sort_keys=True,
    )
    lower = raw_text.lower()
    if not any(
        token in lower
        for token in (
            "official",
            "regulatory",
            "primary",
            "documentation",
            "docs",
            "vendor",
            "provider",
            "browser",
            "source",
            "record",
            "공식",
            "규제",
            "원문",
        )
    ):
        return 0
    named_entities: set[str] = set()
    vendor_tokens = {
        "adobe",
        "airbnb",
        "amazon",
        "anthropic",
        "apple",
        "atlassian",
        "aws",
        "azure",
        "box",
        "chrome",
        "cloudflare",
        "edge",
        "figma",
        "firebase",
        "firefox",
        "github",
        "gitlab",
        "google",
        "gpt",
        "ibm",
        "jira",
        "meta",
        "microsoft",
        "mozilla",
        "notion",
        "openai",
        "oracle",
        "safari",
        "salesforce",
        "slack",
        "teams",
        "vercel",
        "xcode",
    }
    for token in vendor_tokens:
        if re.search(rf"\b{re.escape(token)}\b", lower):
            named_entities.add(token)
    proper_pair_pattern = re.compile(
        r"\b([A-Z][A-Za-z0-9.+#-]{1,})\s*(?:\+|/|,|\bvs\.?\b|\bversus\b|\band\b)\s*([A-Z][A-Za-z0-9.+#-]{1,})\b"
    )
    for left, right in proper_pair_pattern.findall(raw_text):
        named_entities.add(left.lower())
        named_entities.add(right.lower())
    return len(named_entities)


def _semantic_source_cap_text_has_any(text: str, needles: Sequence[str]) -> bool:
    normalized_text = _normalize_text(text)
    lower_text = text.lower()
    for needle in needles:
        lower_needle = str(needle).lower()
        normalized_needle = _normalize_text(lower_needle)
        if lower_needle and lower_needle in lower_text:
            return True
        if normalized_needle and normalized_needle in normalized_text:
            return True
    return False


def _codex_semantic_candidate_validation(
    *,
    original_question: str,
    candidate: Mapping[str, Any],
    visual_preference: str | None = None,
    provided_images: Sequence[Any] | None = None,
) -> dict[str, Any]:
    try:
        validation = validate_semantic_candidate_plan(
            original_question=original_question,
            plan=candidate,
            visual_preference=visual_preference,
            provided_images=provided_images,
        )
    except Exception as exc:  # pragma: no cover - defensive boundary guard
        validation = {
            "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
            "planner_mode": candidate.get("planner_mode"),
            "semantic_release_eligible": bool(
                candidate.get("semantic_release_eligible")
            ),
            "failure_count": 1,
            "failures": [
                {
                    "code": "candidate_validation_exception",
                    "exception_type": exc.__class__.__name__,
                    "message": str(exc),
                }
            ],
            "ok": False,
        }
    return dict(validation)


def _candidate_validation_blocked_reason(validation: Mapping[str, Any]) -> str:
    failures = validation.get("failures")
    failure_codes = [
        str(failure.get("code"))
        for failure in failures
        if isinstance(failure, Mapping) and failure.get("code")
    ] if isinstance(failures, list) else []
    reason = "Codex semantic candidate_plan failed semantic validation"
    if failure_codes:
        reason += ": " + ", ".join(failure_codes[:8])
    return reason


def _codex_semantic_planner_validation_max_attempts() -> int:
    raw_value = os.environ.get(CODEX_SEMANTIC_PLANNER_VALIDATION_MAX_ATTEMPTS_ENV, "3")
    try:
        value = int(raw_value)
    except ValueError:
        return 3
    return max(1, min(value, 3))


def _candidate_validation_failure_codes(validation: Mapping[str, Any]) -> list[str]:
    failures = validation.get("failures")
    if not isinstance(failures, list):
        return []
    return [
        str(failure.get("code"))
        for failure in failures
        if isinstance(failure, Mapping) and failure.get("code")
    ]


def _candidate_validation_attempt_record(
    *,
    attempt: int,
    validation: Mapping[str, Any],
    raw_response: Mapping[str, Any],
    candidate: Mapping[str, Any],
    repair_inputs: Mapping[str, Any] | None = None,
    final_selection: bool = False,
    terminal_failure: bool = False,
) -> dict[str, Any]:
    failure_codes = _candidate_validation_failure_codes(validation)
    candidate_hash = _sha256_payload(candidate)
    return {
        "attempt": attempt,
        "candidate_id": candidate_hash[:16],
        "candidate_hash": candidate_hash,
        "raw_response_hash": _sha256_payload(raw_response),
        "deterministic_ok": validation.get("ok"),
        "deterministic_failure_codes": failure_codes,
        "deterministic_failures": [
            dict(failure)
            for failure in _list(validation.get("failures"))[:8]
            if isinstance(failure, Mapping)
        ],
        "repair_inputs": dict(repair_inputs or {}),
        "final_selection": final_selection,
        "terminal_failure": terminal_failure,
        "candidate_validation_failure_codes": failure_codes,
        "candidate_validation_failure_count": int(
            validation.get("failure_count")
            or len(failure_codes)
        ),
        "candidate_validation": dict(validation),
    }


def _candidate_validation_retry_record(
    *,
    attempt: int,
    validation: Mapping[str, Any],
    raw_response: Mapping[str, Any],
    candidate: Mapping[str, Any],
    retry_request: Mapping[str, Any],
) -> dict[str, Any]:
    repair_inputs = {
        "retry_source": "adapter_candidate_validation",
        "next_attempt": retry_request.get("retry_attempt"),
        "retry_request_hash": retry_request.get("adapter_request_hash"),
        "deterministic_failure_codes": _candidate_validation_failure_codes(validation),
        "deterministic_failures": [
            dict(failure)
            for failure in _list(validation.get("failures"))[:8]
            if isinstance(failure, Mapping)
        ],
    }
    return _candidate_validation_attempt_record(
        attempt=attempt,
        validation=validation,
        raw_response=raw_response,
        candidate=candidate,
        repair_inputs=repair_inputs,
    )


def _codex_semantic_retry_raw_request(
    *,
    raw_request: Mapping[str, Any],
    attempt: int,
    validation: Mapping[str, Any],
) -> dict[str, Any]:
    retry_request = dict(raw_request)
    retry_request.pop("adapter_request_hash", None)
    failure_codes = _candidate_validation_failure_codes(validation)
    retry_request["retry_attempt"] = attempt
    retry_request["previous_candidate_validation_failure_codes"] = failure_codes
    retry_request["previous_candidate_validation_failures"] = [
        dict(failure)
        for failure in _list(validation.get("failures"))[:8]
        if isinstance(failure, Mapping)
    ]
    visual_preference = _normalized_visual_preference(
        retry_request.get("visual_preference")
    )
    if visual_preference == "text_only":
        visual_guidance = _text_only_visual_contract_instruction()
    elif visual_preference == "visual_optional":
        visual_guidance = _visual_optional_contract_instruction(
            has_provided_images=bool(_list(retry_request.get("provided_images"))),
            prompt_mentions_visual=question_mentions_visual_evidence(
                str(retry_request.get("original_question") or "")
            ),
        )
    else:
        visual_guidance = (
            "If the question requires visual evidence, include at least one visual "
            "angle with evidence_need `visual_example` for collecting representative "
            "source images and at least one visual angle with evidence_need "
            "`visual_observation` or `vlm_analysis` for image interpretation. Every "
            "visual route, visual target, image/chart/figure/screenshot artifact, or "
            "visual expected evidence task must set max_images between 1 and 3; "
            "text-only tasks should keep max_images=0."
        )
    semantic_angle_guidance = (
        " If `semantic_angle_release_depth_failed` appears, rewrite the failing "
        "angle titles and research_questions so they are prompt-specific, preserve "
        "the user's subject, modality, source-quality, geography/time, and "
        "output-shape anchors, and share at least 2 meaningful non-generic tokens "
        "with the original question. Avoid generic, copied-original, placeholder, "
        "or shallow angle text."
        if "semantic_angle_release_depth_failed" in failure_codes
        else ""
    )
    semantic_angle_duplicate_guidance = (
        " If `semantic_angle_release_duplicate_failed` appears, produce materially "
        "distinct semantic angles with different evidence needs, source families, "
        "comparison axes, constraints, and report sections. Do not repeat the same "
        "wording with numeric suffixes, reordered phrasing, or synonym-only variants."
        if "semantic_angle_release_duplicate_failed" in failure_codes
        else ""
    )
    repair_guidance = _semantic_repair_guidance_for_codes(
        failure_codes,
        visual_preference=visual_preference,
        provided_images=_list(retry_request.get("provided_images")),
    )
    retry_request["planner_retry_instructions"] = (
        "The previous Codex semantic candidate did not pass release validation. "
        "Generate a fresh semantic decomposition that fixes these failure codes "
        f"without lowering any release gate: {', '.join(failure_codes) or 'unknown'}. "
        "For broad questions, return 5 to 8 distinct semantic angles and 20 to 40 "
        "bounded tasks, with at least 2 bounded_tasks assigned to every angle_id. "
        "If `broad_angle_has_too_few_tasks` appears, repair the per-angle task "
        "distribution by adding or reassigning specific bounded tasks so each broad "
        "angle has at least 2 tasks while the total remains 20 to 40. Preserve the "
        "user's domain, modality, source-quality, "
        f"geography/time, and deliverable requirements. {visual_guidance}"
        f"{semantic_angle_guidance}"
        f"{semantic_angle_duplicate_guidance}"
        f"{repair_guidance}"
    )
    retry_request["adapter_request_hash"] = _sha256_payload(retry_request)
    return retry_request


def _normalized_visual_preference(value: Any) -> str:
    normalized = str(value or "auto").strip().lower()
    if normalized in {"text_only", "visual_required", "visual_optional"}:
        return normalized
    return "auto"


def _text_only_visual_contract_instruction() -> str:
    return (
        "When visual_preference is text_only, all angles and bounded_tasks must use "
        "route=text_only, max_images=0, expected_visual_targets=[], and no image, "
        "chart, screenshot, diagram, visual-example, visual-observation, or VLM work "
        "may be introduced."
    )


def _visual_optional_contract_instruction(
    *,
    has_provided_images: bool = False,
    prompt_mentions_visual: bool = False,
) -> str:
    if has_provided_images or prompt_mentions_visual:
        return (
            "When visual_preference is visual_optional, keep executable visual support "
            "using route=visual_optional with concrete visual targets and max_images "
            "1-3, while keeping text/document/table/structured evidence tasks for the "
            "primary comparison and deliverable obligations."
        )
    return (
        "When visual_preference is visual_optional and the user did not provide an "
        "image or explicitly ask to inspect images/screenshots/figures, keep visual "
        "work bounded and supporting: use route=visual_optional, not visual_required; "
        "include at least one executable visual-support task with concrete visual "
        "targets and max_images 1-3; keep visual support to no more than one quarter "
        "of angles/tasks; and preserve text/document/table/structured-artifact tasks "
        "as the primary comparison and deliverable path."
    )


def _visual_optional_without_primary_visual_evidence(
    *,
    question: str,
    visual_preference: str,
    provided_images: Sequence[Any] | None = None,
    expected_modalities: Sequence[str] | None = None,
) -> bool:
    if visual_preference != "visual_optional":
        return False
    if provided_images:
        return False
    normalized_modalities = {
        _normalize_text(str(item)).replace(" ", "_")
        for item in expected_modalities or []
        if str(item).strip()
    }
    if normalized_modalities & {
        "visual",
        "image",
        "images",
        "screenshot",
        "screenshots",
        "figure",
        "figures",
        "chart",
        "charts",
        "diagram",
        "diagrams",
    }:
        return False
    return not question_mentions_visual_evidence(question)


def _semantic_repair_guidance_for_codes(
    codes: Sequence[str],
    *,
    visual_preference: str | None = None,
    provided_images: Sequence[Any] | None = None,
) -> str:
    code_set = {str(code) for code in codes if str(code).strip()}
    guidance: list[str] = []
    if code_set & {
        "MODALITY_OPTIONALITY_REVERSED",
        "visual_optional_visual_work_dominates_primary_evidence",
    }:
        guidance.append(
            " If `MODALITY_OPTIONALITY_REVERSED` appears, repair visual_optional "
            "by keeping visual work bounded and supporting: use route=visual_optional "
            "rather than route=visual_required, keep at least one executable visual "
            "support task with concrete targets and max_images 1-3, cap visual support "
            "to one quarter of angles/tasks when no user image was supplied, and make "
            "text/document/table/structured-artifact comparison the primary path."
        )
    if code_set & {
        "REQ_003_COMPARISON_DELIVERABLE_INCOMPLETE",
        "comparison_deliverable_missing_required_fields",
    }:
        guidance.append(
            " If `REQ_003_COMPARISON_DELIVERABLE_INCOMPLETE` appears, add a bounded "
            "text/document/structured task and expected artifact for a consolidated "
            "side-by-side comparison deliverable using the entities and comparison "
            "axes from the user's own question. Include fields for requirement or "
            "criterion, each compared item/standard/source named by the user, "
            "match/partial/mismatch/unverifiable status where compliance-style "
            "judgment is requested, evidence, caveats, and remediation or next action "
            "only when the oracle asks for it."
        )
    if code_set & {
        "REQ_003_PRIORITIZED_REMEDIATION_MISSING",
        "prioritized_remediation_missing",
    }:
        guidance.append(
            " If `REQ_003_PRIORITIZED_REMEDIATION_MISSING` appears, add a bounded "
            "task and artifact for prioritized remediation recommendations that rank "
            "mismatches or gaps by severity, impact, evidence confidence, and feasible "
            "next action."
        )
    if code_set & {
        "STRUCTURED_ARTIFACT_ROUTE_INCOMPLETE",
        "structured_artifact_assessment_missing",
    }:
        guidance.append(
            " If `STRUCTURED_ARTIFACT_ROUTE_INCOMPLETE` appears, add text/document/"
            "structured-artifact assessment tasks for model outputs or artifacts: "
            "inventory the files/fields/attributes/object classes/LOD or equivalent "
            "structured properties, compare them against public design and tender "
            "criteria, and avoid making VLM or image inspection mandatory unless the "
            "user supplied an actual image/model file."
        )
    if "bounded_task_requirement_exceeds_max_sources" in code_set:
        guidance.append(
            " If `bounded_task_requirement_exceeds_max_sources` appears, do not "
            "raise any bounded_task.max_sources above 5 and do not collapse the "
            "obligation into an impossible single task. Split the over-broad "
            "source/vendor/jurisdiction/source-artifact obligation into multiple "
            "bounded source-group tasks, each executable within max_sources 1-5, "
            "then synthesize across those groups."
        )
    if code_set & {
        "broad_question_angle_count_out_of_range",
        "broad_question_task_count_out_of_range",
    }:
        guidance.append(
            " If a broad-question count failure appears, repair by preserving the "
            "question as broad and returning 5-8 prompt-specific semantic angles "
            "and 20-40 executable bounded tasks. Do not relabel a broad software, "
            "architecture, migration, testing, comparison, official-source, or "
            "current-docs investigation as narrow."
        )
    if visual_preference == "visual_optional" and not provided_images:
        guidance.append(
            " For this visual_optional retry with no supplied images, do not let "
            "visual acquisition or VLM analysis displace the primary text/document/"
            "structured comparison deliverable."
        )
    return "".join(guidance)


def _append_text_only_visual_work_violations(
    violations: list[dict[str, Any]],
    *,
    record_type: str,
    record: Mapping[str, Any],
    fields: Sequence[str],
) -> None:
    record_details: dict[str, Any] = {"record_type": record_type}
    if record_type == "angle":
        record_details["angle_id"] = record.get("angle_id")
    if record_type == "task":
        record_details["task_id"] = record.get("task_id")
        record_details["angle_id"] = record.get("angle_id")
    for field_name in fields:
        raw_value = record.get(field_name)
        if field_name == "evidence_need":
            evidence_need = str(raw_value or "").strip()
            if evidence_need in VISUAL_EXPECTED_EVIDENCE:
                violations.append(
                    {
                        **record_details,
                        "field": field_name,
                        "value": evidence_need,
                        "matches": [evidence_need],
                        "reason": "visual_evidence_need",
                    }
                )
            continue
        for value in _text_only_visual_work_field_values(raw_value):
            matches = _text_only_visual_work_matches(value)
            if not matches:
                continue
            violations.append(
                {
                    **record_details,
                    "field": field_name,
                    "value": _preview_text(str(value)),
                    "matches": matches,
                    "reason": "visual_work_term",
                }
            )


def _text_only_visual_work_field_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [json.dumps(value, ensure_ascii=False, sort_keys=True)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values: list[str] = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, Mapping):
                values.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
            else:
                values.append(str(item))
        return values
    return [str(value)]


def _text_only_visual_work_matches(value: str) -> list[str]:
    text = _normalize_text(str(value or "")).lower()
    if not text:
        return []
    matches: list[str] = []
    for label, pattern in TEXT_ONLY_VISUAL_WORK_TEXT_PATTERNS:
        for match in re.finditer(pattern, text):
            if _text_only_visual_work_match_negated(text, match.start()):
                continue
            matches.append(label)
            break
    return list(dict.fromkeys(matches))


def _text_only_visual_work_match_negated(text: str, start_index: int) -> bool:
    prefix = text[max(0, start_index - 48):start_index]
    return TEXT_ONLY_VISUAL_WORK_NEGATION_PATTERN.search(prefix) is not None


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


def write_semantic_expectation_oracle(
    *,
    run_dir: str | Path,
    question: str,
    user_constraints: Sequence[str] | None = None,
    depth_preset: str = "standard",
    visual_preference: str | None = None,
    budget_cap: Mapping[str, Any] | None = None,
    provided_sources: Sequence[Mapping[str, Any]] | None = None,
    provided_images: Sequence[Mapping[str, Any]] | None = None,
    created_at: str | None = None,
    manifest_oracle_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write and lock the semantic expectation oracle before planning."""

    run_path = Path(run_dir)
    timestamp = created_at or _utc_now_from_run(run_path)
    raw_dir = run_path / SEMANTIC_ORACLE_RAW_DIRNAME
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_request_path = raw_dir / SEMANTIC_ORACLE_RAW_REQUEST_FILENAME
    raw_response_path = raw_dir / SEMANTIC_ORACLE_RAW_RESPONSE_FILENAME
    raw_request = _semantic_oracle_raw_request(
        run_id=run_path.name,
        question=question,
        user_constraints=user_constraints or [],
        depth_preset=depth_preset,
        visual_preference=visual_preference,
        budget_cap=budget_cap or {},
        provided_sources=provided_sources or [],
        provided_images=provided_images or [],
        created_at=timestamp,
    )
    raw_request_content_hash = _sha256_payload(raw_request)
    raw_request["raw_request_content_hash"] = raw_request_content_hash
    raw_request["raw_request_hash"] = raw_request_content_hash
    _write_json(raw_request_path, raw_request)
    raw_request_artifact_hash = _sha256_file(raw_request_path)
    try:
        adapter_response = invoke_codex_semantic_oracle_adapter(raw_request)
        adapter_unavailable_reason = None
    except SemanticPlannerAdapterUnavailable as exc:
        adapter_response = None
        adapter_unavailable_reason = str(exc) or "semantic oracle adapter unavailable"
    if adapter_response is None:
        raw_response = _deterministic_oracle_raw_response(
            request=raw_request,
            raw_request_hash=raw_request_content_hash,
            unavailable_reason=adapter_unavailable_reason,
        )
    else:
        raw_response = _structured_semantic_oracle_response(
            raw_request=raw_request,
            raw_request_hash=raw_request_content_hash,
            adapter_response=adapter_response,
        )
    _apply_manifest_oracle_binding(raw_response, manifest_oracle_binding)
    raw_response["run_id"] = run_path.name
    raw_response["created_at"] = timestamp
    raw_response["raw_request_content_hash"] = raw_request_content_hash
    raw_response["raw_request_artifact_hash"] = raw_request_artifact_hash
    raw_response["raw_request_hash"] = raw_request_content_hash
    _write_json(raw_response_path, raw_response)
    raw_response_hash = _sha256_file(raw_response_path)
    oracle = _expectation_oracle_from_raw_response(
        run_path=run_path,
        question=question,
        raw_request_path=raw_request_path,
        raw_response_path=raw_response_path,
        raw_request_content_hash=raw_request_content_hash,
        raw_request_artifact_hash=raw_request_artifact_hash,
        raw_response_hash=raw_response_hash,
        raw_response=raw_response,
        timestamp=timestamp,
    )
    _write_json(run_path / SEMANTIC_EXPECTATION_ORACLE_FILENAME, oracle)
    return oracle


def invoke_codex_semantic_oracle_adapter(
    request: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Invoke the independent Codex semantic expectation-oracle boundary."""

    return _invoke_codex_semantic_json_adapter(
        request=request,
        command_env=CODEX_SEMANTIC_ORACLE_COMMAND_ENV,
        timeout_env=CODEX_SEMANTIC_ORACLE_TIMEOUT_ENV,
        role_label="semantic oracle",
    )


def write_semantic_plan_review(
    *,
    run_dir: str | Path,
    question: str,
    plan: SemanticPlan,
    oracle: Mapping[str, Any],
    created_at: str | None = None,
    manifest_oracle_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Review a semantic plan against the locked oracle and write review artifacts."""

    run_path = Path(run_dir)
    timestamp = created_at or _utc_now_from_run(run_path)
    raw_dir = run_path / SEMANTIC_REVIEWER_RAW_DIRNAME
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_request_path = raw_dir / SEMANTIC_REVIEWER_RAW_REQUEST_FILENAME
    raw_response_path = raw_dir / SEMANTIC_REVIEWER_RAW_RESPONSE_FILENAME
    oracle_hash = _sha256_file(run_path / SEMANTIC_EXPECTATION_ORACLE_FILENAME)
    plan_payload = plan.to_dict()
    plan_hash = _sha256_text(json.dumps(plan_payload, sort_keys=True, ensure_ascii=True))
    raw_request = {
        "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
        "artifact_type": "semantic_reviewer_raw_request",
        "run_id": run_path.name,
        "created_at": timestamp,
        "reviewer_adapter": CODEX_SEMANTIC_REVIEWER_ADAPTER_NAME,
        "prompt_version": CODEX_SEMANTIC_REVIEWER_PROMPT_VERSION,
        "original_question": question,
        "semantic_expectation_oracle_path": SEMANTIC_EXPECTATION_ORACLE_FILENAME,
        "semantic_expectation_oracle_hash": oracle_hash,
        "semantic_plan_candidate_hash": plan_hash,
        "semantic_plan": plan_payload,
        "review_instructions": [
            "Compare the plan to the locked expectation oracle.",
            "Reject reverse-fit, hidden template, generic wrapper, or substitute implementation behavior.",
            "Require complete non-negotiable coverage, modality/source constraints, and bounded executable tasks.",
        ],
        "response_schema_shape": {
            "semantic_fit_score": "number 0-10",
            "score_dimensions": "object",
            "blockers": "list",
            "warnings": "list",
            "substitute_implementation_check": "object",
            "verdict": "pass|fail|release_ineligible",
        },
    }
    raw_request_content_hash = _sha256_payload(raw_request)
    raw_request["raw_request_content_hash"] = raw_request_content_hash
    raw_request["raw_request_hash"] = raw_request_content_hash
    _write_json(raw_request_path, raw_request)
    raw_request_artifact_hash = _sha256_file(raw_request_path)
    try:
        adapter_response = invoke_codex_semantic_reviewer_adapter(raw_request)
        adapter_unavailable_reason = None
    except SemanticPlannerAdapterUnavailable as exc:
        adapter_response = None
        adapter_unavailable_reason = str(exc) or "semantic reviewer adapter unavailable"
    if adapter_response is None:
        raw_response = _deterministic_review_raw_response(
            request=raw_request,
            plan=plan,
            oracle=oracle,
            raw_request_hash=raw_request_content_hash,
            unavailable_reason=adapter_unavailable_reason,
        )
    else:
        raw_response = _structured_semantic_reviewer_response(
            raw_request=raw_request,
            raw_request_hash=raw_request_content_hash,
            adapter_response=adapter_response,
        )
    _apply_manifest_oracle_binding(raw_response, manifest_oracle_binding)
    raw_response["run_id"] = run_path.name
    raw_response["created_at"] = timestamp
    raw_response["raw_request_content_hash"] = raw_request_content_hash
    raw_response["raw_request_artifact_hash"] = raw_request_artifact_hash
    raw_response["raw_request_hash"] = raw_request_content_hash
    raw_response["semantic_plan_candidate_hash"] = plan_hash
    _write_json(raw_response_path, raw_response)
    raw_response_hash = _sha256_file(raw_response_path)
    review = _semantic_plan_review_from_raw_response(
        run_path=run_path,
        question=question,
        plan=plan,
        oracle=oracle,
        raw_request_path=raw_request_path,
        raw_response_path=raw_response_path,
        raw_request_content_hash=raw_request_content_hash,
        raw_request_artifact_hash=raw_request_artifact_hash,
        raw_response_hash=raw_response_hash,
        raw_response=raw_response,
        timestamp=timestamp,
    )
    _write_json(run_path / SEMANTIC_PLAN_REVIEW_FILENAME, review)
    return review


def _apply_manifest_oracle_binding(
    payload: dict[str, Any],
    binding: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(binding, Mapping):
        return payload
    clean = {
        field: str(binding.get(field) or "").strip()
        for field in (
            "manifest_oracle_hash",
            "manifest_oracle_path",
            "manifest_oracle_fragment_id",
        )
    }
    if not all(clean.values()):
        return payload
    payload.update(clean)
    for provenance_field in (
        "provenance",
        "oracle_provenance",
        "reviewer_provenance",
        "planner_provenance",
    ):
        provenance = payload.get(provenance_field)
        if isinstance(provenance, Mapping):
            merged = dict(provenance)
            merged.update(clean)
            payload[provenance_field] = merged
    return payload


def invoke_codex_semantic_reviewer_adapter(
    request: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Invoke the independent Codex semantic fit reviewer boundary."""

    return _invoke_codex_semantic_json_adapter(
        request=request,
        command_env=CODEX_SEMANTIC_REVIEWER_COMMAND_ENV,
        timeout_env=CODEX_SEMANTIC_REVIEWER_TIMEOUT_ENV,
        role_label="semantic reviewer",
    )


def semantic_review_release_eligible(review: Mapping[str, Any]) -> bool:
    """Return whether a review satisfies the P3-SP3 release gate."""

    try:
        score = float(review.get("semantic_fit_score"))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(score) or score < SEMANTIC_FIT_SCORE_THRESHOLD:
        return False
    blockers = review.get("blockers")
    if not isinstance(blockers, list) or blockers:
        return False
    if review.get("verdict") != "pass" and review.get("final_verdict") != "pass":
        return False
    if review.get("non_negotiable_coverage_complete") is not True:
        return False
    substitute = review.get("substitute_implementation_check")
    if not isinstance(substitute, Mapping) or substitute.get("passed") is not True:
        return False
    independence = review.get("reviewer_independence")
    if not isinstance(independence, Mapping) or independence.get("independent") is not True:
        return False
    return True


def semantic_plan_with_review_result(
    plan: SemanticPlan,
    review: Mapping[str, Any],
) -> SemanticPlan:
    """Return a plan annotated with the semantic review release decision."""

    eligible = semantic_review_release_eligible(review)
    diagnostics = dict(plan.diagnostics or {})
    failed_diagnostic = (
        "The semantic plan failed independent semantic review and cannot fan out."
        if plan.planner_mode == PLANNER_MODE_CODEX_SEMANTIC
        else diagnostics.get("user_visible_diagnostic")
        or "True semantic decomposition did not run; this path is useful only as a release-ineligible fallback and cannot satisfy semantic planner gates."
    )
    diagnostics.update(
        {
            "semantic_release_eligible": eligible,
            "semantic_fit_score": review.get("semantic_fit_score"),
            "semantic_review_verdict": review.get("verdict") or review.get("final_verdict"),
            "semantic_review_blocker_count": len(_list(review.get("blockers"))),
            "user_visible_diagnostic": (
                "The semantic plan passed independent oracle review."
                if eligible
                else failed_diagnostic
            ),
        }
    )
    return replace(
        plan,
        semantic_release_eligible=eligible,
        status="semantic_review_passed" if eligible else "blocked_semantic_review_failed",
        diagnostics=diagnostics,
    )


def semantic_plan_candidate_hash(plan: SemanticPlan | Mapping[str, Any]) -> str:
    payload = plan.to_dict() if isinstance(plan, SemanticPlan) else dict(plan)
    return _sha256_text(json.dumps(payload, sort_keys=True, ensure_ascii=True))


def semantic_plan_candidate_validation(plan: SemanticPlan | Mapping[str, Any]) -> dict[str, Any]:
    """Extract deterministic candidate validation from normal or blocked payloads."""

    if isinstance(plan, SemanticPlan):
        raw_response = plan.raw_response_payload
        diagnostics = plan.diagnostics
    else:
        raw_response = dict(plan).get("raw_response_payload")
        diagnostics = dict(plan).get("diagnostics")
    for payload in (
        raw_response,
        raw_response.get("adapter_response") if isinstance(raw_response, Mapping) else None,
        raw_response.get("diagnostics") if isinstance(raw_response, Mapping) else None,
        diagnostics,
    ):
        if not isinstance(payload, Mapping):
            continue
        candidate_validation = payload.get("candidate_validation")
        if isinstance(candidate_validation, Mapping):
            return dict(candidate_validation)
    return {}


def semantic_plan_adapter_candidate_attempts(
    plan: SemanticPlan | Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Extract structured adapter-level candidate attempts for convergence records."""

    if isinstance(plan, SemanticPlan):
        raw_response = plan.raw_response_payload
        diagnostics = plan.diagnostics
    else:
        raw_response = dict(plan).get("raw_response_payload")
        diagnostics = dict(plan).get("diagnostics")
    payloads = (
        raw_response,
        raw_response.get("adapter_response") if isinstance(raw_response, Mapping) else None,
        raw_response.get("diagnostics") if isinstance(raw_response, Mapping) else None,
        diagnostics,
    )
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        attempts = payload.get("adapter_candidate_attempts")
        if not isinstance(attempts, list):
            continue
        normalized = [
            _normalize_adapter_candidate_attempt_record(attempt)
            for attempt in attempts
            if isinstance(attempt, Mapping)
        ]
        if normalized:
            return normalized
    return []


def _normalize_adapter_candidate_attempt_record(
    attempt: Mapping[str, Any],
) -> dict[str, Any]:
    candidate_hash = str(attempt.get("candidate_hash") or "")
    candidate_id = str(attempt.get("candidate_id") or candidate_hash[:16])
    deterministic_failures = attempt.get("deterministic_failures")
    if not isinstance(deterministic_failures, list):
        deterministic_failures = attempt.get("candidate_validation", {}).get(
            "failures",
            [],
        ) if isinstance(attempt.get("candidate_validation"), Mapping) else []
    failure_codes = attempt.get("deterministic_failure_codes")
    if not isinstance(failure_codes, list):
        failure_codes = attempt.get("candidate_validation_failure_codes", [])
    return {
        "attempt": attempt.get("attempt"),
        "candidate_id": candidate_id,
        "candidate_hash": candidate_hash,
        "raw_response_hash": attempt.get("raw_response_hash"),
        "deterministic_ok": attempt.get("deterministic_ok"),
        "deterministic_failure_codes": [str(code) for code in _list(failure_codes)],
        "deterministic_failures": [
            dict(failure)
            for failure in _list(deterministic_failures)
            if isinstance(failure, Mapping)
        ],
        "repair_inputs": (
            dict(attempt.get("repair_inputs"))
            if isinstance(attempt.get("repair_inputs"), Mapping)
            else {}
        ),
        "final_selection": bool(attempt.get("final_selection")),
        "terminal_failure": bool(attempt.get("terminal_failure")),
    }


def semantic_review_failure_codes(review: Mapping[str, Any]) -> list[str]:
    codes: list[str] = []
    for blocker in _list(review.get("blockers")):
        if isinstance(blocker, Mapping) and blocker.get("code"):
            codes.append(str(blocker["code"]))
        elif isinstance(blocker, str) and blocker.strip():
            codes.append(blocker.strip())
    score = _numeric_score(review.get("semantic_fit_score"))
    if score is None:
        codes.append("semantic_fit_score_missing_or_non_finite")
    elif score < SEMANTIC_FIT_SCORE_THRESHOLD:
        codes.append("semantic_fit_score_below_threshold")
    if review.get("verdict") != "pass" and review.get("final_verdict") != "pass":
        codes.append("semantic_review_verdict_not_pass")
    if review.get("non_negotiable_coverage_complete") is not True:
        codes.append("non_negotiable_coverage_incomplete")
    substitute = review.get("substitute_implementation_check")
    if not isinstance(substitute, Mapping) or substitute.get("passed") is not True:
        codes.append("substitute_implementation_check_failed")
    return list(dict.fromkeys(codes))


NON_RETRYABLE_SEMANTIC_REVIEW_FAILURE_CODES = {
    "reviewer_adapter_unavailable",
    "non_release_reviewer_fixture",
    "non_release_oracle_fixture",
    "reviewer_or_oracle_provenance_not_independent",
    "semantic_reviewer_non_release_fixture",
    "reviewer_raw_request_path_missing",
    "reviewer_raw_response_path_missing",
    "reviewer_provenance_incomplete",
}


def semantic_review_failure_retryable(review: Mapping[str, Any]) -> bool:
    """Return whether reviewer blockers can be repaired by a fresh planner candidate."""

    if semantic_review_release_eligible(review):
        return False
    codes = set(semantic_review_failure_codes(review))
    if not codes:
        return False
    return not bool(codes & NON_RETRYABLE_SEMANTIC_REVIEW_FAILURE_CODES)


def semantic_convergence_attempt_record(
    *,
    attempt: int,
    plan: SemanticPlan,
    deterministic_validation: Mapping[str, Any] | None,
    semantic_review: Mapping[str, Any] | None,
    repair_inputs: Mapping[str, Any] | None = None,
    final_selection: bool = False,
    terminal_failure: bool = False,
) -> dict[str, Any]:
    candidate_hash = semantic_plan_candidate_hash(plan)
    deterministic = dict(deterministic_validation or {})
    review = dict(semantic_review or {})
    adapter_candidate_attempts = semantic_plan_adapter_candidate_attempts(plan)
    return {
        "attempt": attempt,
        "candidate_id": candidate_hash[:16],
        "candidate_hash": candidate_hash,
        "planner_status": plan.status,
        "planner_mode": plan.planner_mode,
        "raw_request_hash": plan.planner_provenance.get("raw_request_hash")
        or plan.planner_provenance.get("adapter_request_hash"),
        "raw_response_hash": plan.planner_provenance.get("raw_response_hash"),
        "deterministic_ok": deterministic.get("ok"),
        "deterministic_failure_codes": _candidate_validation_failure_codes(
            deterministic
        ),
        "deterministic_failures": [
            dict(failure)
            for failure in _list(deterministic.get("failures"))
            if isinstance(failure, Mapping)
        ],
        "reviewer_semantic_fit_score": review.get("semantic_fit_score"),
        "reviewer_verdict": review.get("verdict") or review.get("final_verdict"),
        "reviewer_release_eligible": semantic_review_release_eligible(review)
        if review
        else False,
        "reviewer_blocker_codes": semantic_review_failure_codes(review)
        if review
        else [],
        "reviewer_blockers": [
            dict(blocker)
            for blocker in _list(review.get("blockers"))
            if isinstance(blocker, Mapping)
        ],
        "adapter_candidate_attempt_count": len(adapter_candidate_attempts),
        "adapter_candidate_attempts": adapter_candidate_attempts,
        "repair_inputs": dict(repair_inputs or {}),
        "final_selection": final_selection,
        "terminal_failure": terminal_failure,
    }


def semantic_convergence_artifact(
    *,
    run_id: str,
    max_attempts: int,
    attempts: Sequence[Mapping[str, Any]],
    final_plan: SemanticPlan,
    final_review: Mapping[str, Any] | None,
    status: str,
) -> dict[str, Any]:
    final_candidate_hash = semantic_plan_candidate_hash(final_plan)
    review = dict(final_review or {})
    terminal_codes = []
    if status != "converged":
        terminal_codes = (
            semantic_review_failure_codes(review)
            if isinstance(final_review, Mapping)
            else []
        )
        if not terminal_codes:
            candidate_validation = semantic_plan_candidate_validation(final_plan)
            terminal_codes = _candidate_validation_failure_codes(candidate_validation)
        if not terminal_codes:
            diagnostics = final_plan.diagnostics if isinstance(final_plan.diagnostics, Mapping) else {}
            blocked_reason = diagnostics.get("blocked_reason") or diagnostics.get(
                "adapter_invalid_response_reason"
            )
            if blocked_reason:
                terminal_codes = [str(blocked_reason)]
    return {
        "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
        "artifact_type": "semantic_planner_convergence",
        "run_id": run_id,
        "status": status,
        "max_attempts": max_attempts,
        "attempt_count": len(attempts),
        "attempts": [dict(attempt) for attempt in attempts],
        "final_selection": (
            {
                "attempt": len(attempts),
                "candidate_id": final_candidate_hash[:16],
                "candidate_hash": final_candidate_hash,
                "semantic_fit_score": review.get("semantic_fit_score"),
                "reviewer_verdict": review.get("verdict") or review.get("final_verdict"),
            }
            if status == "converged"
            else None
        ),
        "terminal_failure": (
            {
                "candidate_id": final_candidate_hash[:16],
                "candidate_hash": final_candidate_hash,
                "reason_codes": terminal_codes,
                "semantic_fit_score": review.get("semantic_fit_score"),
                "reviewer_verdict": review.get("verdict") or review.get("final_verdict"),
            }
            if status != "converged"
            else None
        ),
    }


def codex_semantic_review_retry_raw_request(
    *,
    raw_request: Mapping[str, Any],
    attempt: int,
    deterministic_validation: Mapping[str, Any],
    semantic_review: Mapping[str, Any],
    candidate_hash: str,
    max_attempts: int,
) -> dict[str, Any]:
    retry_request = _codex_semantic_retry_raw_request(
        raw_request=raw_request,
        attempt=attempt,
        validation=deterministic_validation,
    )
    review_codes = semantic_review_failure_codes(semantic_review)
    review_blockers = [
        dict(blocker)
        for blocker in _list(semantic_review.get("blockers"))[:8]
        if isinstance(blocker, Mapping)
    ]
    repair_inputs = {
        "previous_candidate_hash": candidate_hash,
        "deterministic_failure_codes": _candidate_validation_failure_codes(
            deterministic_validation
        ),
        "deterministic_failures": [
            dict(failure)
            for failure in _list(deterministic_validation.get("failures"))[:8]
            if isinstance(failure, Mapping)
        ],
        "reviewer_failure_codes": review_codes,
        "reviewer_blockers": review_blockers,
        "reviewer_semantic_fit_score": semantic_review.get("semantic_fit_score"),
        "reviewer_verdict": semantic_review.get("verdict")
        or semantic_review.get("final_verdict"),
    }
    retry_request["semantic_convergence_attempt"] = attempt
    retry_request["semantic_convergence_max_attempts"] = max_attempts
    retry_request["previous_semantic_review_failure_codes"] = review_codes
    retry_request["previous_semantic_review_blockers"] = review_blockers
    retry_request["previous_semantic_review_score"] = semantic_review.get(
        "semantic_fit_score"
    )
    retry_request["previous_semantic_review_verdict"] = (
        semantic_review.get("verdict") or semantic_review.get("final_verdict")
    )
    retry_request["semantic_convergence_repair_inputs"] = repair_inputs
    retry_request["planner_retry_instructions"] = (
        str(retry_request.get("planner_retry_instructions") or "")
        + " Independent semantic review also failed. Repair using the structured "
        "semantic_convergence_repair_inputs: fix reviewer blockers and score "
        f"failures without lowering the semantic_fit_score threshold {SEMANTIC_FIT_SCORE_THRESHOLD}. "
        "Split over-broad visual, vendor, jurisdiction, or comparison tasks into "
        "smaller bounded tasks when distinct obligations exceed per-task caps; "
        "otherwise raise max_sources only up to 5 and max_images only up to 3 "
        "and only within the supplied budget_cap. Preserve runner-level budgets "
        "as executable reuse constraints. Preserve visual-required/optional work "
        "for visual prompts and preserve text_only constraints when requested. "
        "For ambiguous architecture, model, or testing prompts, use software/Codex "
        "implementation templates only when the original question explicitly asks "
        "about software, Codex, APIs, code, runners, or repositories."
        + _semantic_repair_guidance_for_codes(
            review_codes,
            visual_preference=_normalized_visual_preference(
                retry_request.get("visual_preference")
            ),
            provided_images=_list(retry_request.get("provided_images")),
        )
    )
    retry_request["adapter_request_hash"] = _sha256_payload(retry_request)
    return retry_request


def codex_semantic_candidate_validation_retry_raw_request(
    *,
    raw_request: Mapping[str, Any],
    attempt: int,
    deterministic_validation: Mapping[str, Any],
    candidate_hash: str,
    max_attempts: int,
) -> dict[str, Any]:
    retry_request = _codex_semantic_retry_raw_request(
        raw_request=raw_request,
        attempt=attempt,
        validation=deterministic_validation,
    )
    repair_inputs = {
        "previous_candidate_hash": candidate_hash,
        "deterministic_failure_codes": _candidate_validation_failure_codes(
            deterministic_validation
        ),
        "deterministic_failures": [
            dict(failure)
            for failure in _list(deterministic_validation.get("failures"))[:8]
            if isinstance(failure, Mapping)
        ],
        "retry_source": "adapter_invalid_response_candidate_validation",
    }
    retry_request["semantic_convergence_attempt"] = attempt
    retry_request["semantic_convergence_max_attempts"] = max_attempts
    retry_request["semantic_convergence_repair_inputs"] = repair_inputs
    retry_request["planner_retry_instructions"] = (
        str(retry_request.get("planner_retry_instructions") or "")
        + " The previous semantic planner adapter response was structurally "
        "consumable but failed deterministic candidate validation. Treat these "
        "failures as repairable convergence inputs; preserve user obligations, "
        "split over-broad source or visual work when caps make a single bounded "
        "task infeasible, and only raise max_sources/max_images within schema and "
        "runner budget limits."
    )
    retry_request["adapter_request_hash"] = _sha256_payload(retry_request)
    return retry_request


def _run_codex_semantic_adapter_command_with_capacity_retry(
    *,
    command: Sequence[str],
    request: Mapping[str, Any],
    timeout_seconds: float,
    role_label: str,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    request_json = json.dumps(dict(request), ensure_ascii=False, sort_keys=True)
    max_attempts = _codex_semantic_adapter_capacity_retry_attempts()
    backoff_base_seconds = _codex_semantic_adapter_capacity_retry_backoff_seconds()
    failed_attempts: list[dict[str, Any]] = []
    for attempt in range(1, max_attempts + 1):
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            input=request_json,
            text=True,
            timeout=timeout_seconds,
        )
        if completed.returncode == 0:
            if not failed_attempts:
                return completed, None
            return completed, {
                "retry_policy": "codex_semantic_adapter_capacity_backoff",
                "transient_failure_type": "model_capacity",
                "attempt_count": attempt,
                "failed_attempt_count": len(failed_attempts),
                "successful_attempt": attempt,
                "role_label": role_label,
                "failed_attempts": list(failed_attempts),
            }
        if not _codex_semantic_adapter_completed_process_is_capacity_failure(completed):
            return completed, None
        will_retry = attempt < max_attempts
        backoff_seconds = (
            min(30.0, backoff_base_seconds * (2 ** (attempt - 1)))
            if will_retry
            else 0.0
        )
        failed_attempts.append(
            {
                "attempt": attempt,
                "failure_category": "model_capacity",
                "returncode": completed.returncode,
                "stderr_preview": _preview_text(completed.stderr),
                "stdout_preview": _preview_text(completed.stdout),
                "will_retry": will_retry,
                "backoff_seconds": backoff_seconds,
            }
        )
        if not will_retry:
            raise SemanticPlannerAdapterUnavailable(
                f"Codex {role_label} command exited {completed.returncode} after "
                f"{max_attempts} transient model-capacity attempts; "
                f"stderr={_preview_text(completed.stderr)}; stdout={_preview_text(completed.stdout)}"
            )
        if backoff_seconds > 0:
            time.sleep(backoff_seconds)
    raise SemanticPlannerAdapterUnavailable(
        f"Codex {role_label} command did not return after capacity retry handling"
    )


def _codex_semantic_adapter_capacity_retry_attempts() -> int:
    raw_value = os.environ.get(
        CODEX_SEMANTIC_ADAPTER_CAPACITY_RETRY_ATTEMPTS_ENV,
        "3",
    )
    try:
        value = int(str(raw_value).strip())
    except ValueError:
        return 3
    return max(1, min(value, 5))


def _codex_semantic_adapter_capacity_retry_backoff_seconds() -> float:
    raw_value = os.environ.get(
        CODEX_SEMANTIC_ADAPTER_CAPACITY_RETRY_BACKOFF_SECONDS_ENV,
        "1.0",
    )
    try:
        value = float(str(raw_value).strip())
    except ValueError:
        return 1.0
    if not math.isfinite(value):
        return 1.0
    return max(0.0, min(value, 30.0))


def _codex_semantic_adapter_completed_process_is_capacity_failure(
    completed: subprocess.CompletedProcess[str],
) -> bool:
    text = " ".join(
        (
            str(completed.stderr or ""),
            str(completed.stdout or ""),
        )
    ).lower()
    return (
        "selected model is at capacity" in text
        or "model is at capacity" in text
        or "at capacity. please try a different model" in text
    )


def _invoke_codex_semantic_json_adapter(
    *,
    request: Mapping[str, Any],
    command_env: str,
    timeout_env: str,
    role_label: str,
) -> Mapping[str, Any] | None:
    command = _semantic_adapter_command(command_env=command_env, role_label=role_label)
    if command is None:
        return None
    command_boundary = validate_codex_semantic_adapter_command(command)
    timeout_seconds = float(os.environ.get(timeout_env, "300"))
    completed, retry_metadata = _run_codex_semantic_adapter_command_with_capacity_retry(
        command=command,
        request=request,
        timeout_seconds=timeout_seconds,
        role_label=role_label,
    )
    if completed.returncode != 0:
        raise SemanticPlannerAdapterUnavailable(
            f"Codex {role_label} command exited {completed.returncode}; "
            f"stderr={_preview_text(completed.stderr)}; stdout={_preview_text(completed.stdout)}"
        )
    payload, codex_events = _parse_codex_exec_json_output(completed.stdout)
    provenance = dict(payload.get("provenance") or {})
    provenance.setdefault("adapter_command", _redacted_command(command))
    provenance.setdefault(
        "adapter_invocation_kind",
        command_boundary["adapter_invocation_kind"],
    )
    _force_parent_raw_request_hash(
        provenance,
        request.get("raw_request_hash") or request.get("adapter_request_hash"),
    )
    for key, value in _codex_event_provenance(codex_events).items():
        provenance.setdefault(key, value)
    if retry_metadata:
        provenance["adapter_retry_metadata"] = retry_metadata
    payload = dict(payload)
    if retry_metadata:
        payload["adapter_retry_metadata"] = retry_metadata
    payload["provenance"] = provenance
    return payload


def _force_parent_raw_request_hash(
    provenance: dict[str, Any],
    parent_raw_request_hash: Any,
) -> None:
    """Treat raw request identity as parent-owned, not model-generated."""

    request_hash = str(parent_raw_request_hash or "").strip()
    if not request_hash:
        return
    child_hash = str(provenance.get("raw_request_hash") or "").strip()
    if child_hash and child_hash != request_hash:
        provenance.setdefault("child_reported_raw_request_hash", child_hash)
        provenance.setdefault("raw_request_hash_overridden_by_parent", True)
    provenance["raw_request_hash"] = request_hash


def _semantic_adapter_command(
    *,
    command_env: str,
    role_label: str,
) -> list[str] | None:
    command_text = os.environ.get(command_env, "").strip()
    if command_text:
        command = shlex.split(command_text)
        return command or None
    return _default_codex_semantic_adapter_command(role_label=role_label)


def _default_codex_semantic_adapter_command(*, role_label: str) -> list[str] | None:
    if _semantic_adapter_env_flag_enabled(CODEX_SEMANTIC_DISABLE_DEFAULT_ADAPTER_ENV):
        return None
    if not _semantic_adapter_env_flag_enabled(CODEX_SEMANTIC_ENABLE_DEFAULT_ADAPTER_ENV):
        return None
    codex_binary = shutil.which("codex")
    if not codex_binary:
        return None
    schema_path = _semantic_adapter_schema_path(role_label)
    if not schema_path.exists():
        raise SemanticPlannerAdapterUnavailable(
            f"Codex {role_label} output schema is missing: {schema_path}"
        )
    return [
        codex_binary,
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-C",
        str(_semantic_adapter_workdir()),
        "--output-schema",
        str(schema_path),
        _semantic_adapter_prompt(role_label),
    ]


def _semantic_adapter_schema_path(role_label: str) -> Path:
    filename_by_role = {
        "semantic planner": CODEX_SEMANTIC_PLANNER_SCHEMA_FILENAME,
        "semantic oracle": CODEX_SEMANTIC_ORACLE_SCHEMA_FILENAME,
        "semantic reviewer": CODEX_SEMANTIC_REVIEWER_SCHEMA_FILENAME,
    }
    filename = filename_by_role.get(role_label)
    if not filename:
        raise SemanticPlannerAdapterUnavailable(f"Unknown semantic adapter role: {role_label}")
    return (
        Path(__file__).resolve().parents[2]
        / "validation"
        / CODEX_SEMANTIC_ADAPTER_SCHEMA_DIRNAME
        / filename
    )


def _semantic_adapter_workdir() -> Path:
    configured = os.environ.get(CODEX_SEMANTIC_ADAPTER_WORKDIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    try:
        return Path.cwd().resolve()
    except OSError:
        return Path(__file__).resolve().parents[2]


def _semantic_adapter_prompt(role_label: str) -> str:
    if role_label == "semantic planner":
        return (
            "You are the Codex DeepResearch semantic planner. Read the JSON request from stdin. "
            "Return only JSON matching the provided schema. Decompose the original user question "
            "semantically, not by keyword or fixed template. Use this canonical requirement id "
            "scheme exactly in requirement_coverage_map: req_001 main user subject/domain/entity "
            "scope; req_002 required source quality and official/primary evidence constraints; "
            "req_003 requested analysis/comparison/output shape; req_004 modality, geography, time, "
            "and scope filters that change what evidence is needed; req_005 caveats, contradiction "
            "checks, freshness/currentness, limits, and unknowns. Always include req_001 through "
            "req_005 exactly once, in order. Mark each coverage_status covered and cover every id "
            "with at least one angle and one task. For broad questions produce 5 to 8 angles and "
            "20 to 40 bounded_tasks; every angle must have at least two executable bounded_tasks. "
            "For narrow questions produce 6 to 12 bounded_tasks. Every bounded_task must have "
            "max_sources as an integer from 1 to 5. Text-only tasks must have max_images=0. "
            "Visual-required or visual-optional tasks must have max_images from 1 to 3 and "
            "non-empty expected_visual_targets. "
            f"{_visual_optional_contract_instruction()} "
            "Every angle title plus research_question must be prompt-specific and share at least "
            "2 meaningful non-generic domain/source/comparison tokens with the original question; "
            "avoid generic labels such as comparison, implications, latestness, or limitations "
            "unless the title also names the user's actual domain entities. "
            f"{_text_only_visual_contract_instruction()} "
            "Do not copy angle titles as task queries. Make "
            "every task executable with source policy, concrete success criteria, expected artifacts, "
            "and done_condition. Preserve the user intent, language, domain, modality, geography/time "
            "constraints, and requested deliverable. Do not drift into Codex internals unless the user "
            "asked about Codex."
        )
    if role_label == "semantic oracle":
        return (
            "You are the Codex DeepResearch independent semantic expectation oracle. Read the JSON "
            "request from stdin. Return only JSON matching the provided schema. Build the oracle only "
            "from the original user question and request constraints; do not inspect or infer from any "
            "planner output. Use this canonical requirement id scheme exactly in oracle_requirement_map: "
            "req_001 main user subject/domain/entity scope; req_002 required source quality and "
            "official/primary evidence constraints; req_003 requested analysis/comparison/output shape; "
            "req_004 modality, geography, time, and scope filters that change what evidence is needed; "
            "req_005 caveats, contradiction checks, freshness/currentness, limits, and unknowns. Always "
            "include req_001 through req_005 exactly once, in order. Capture explicit and justified "
            "inferred requirements, including source-quality needs, modality needs, geography/time "
            "constraints, report shape, forbidden drift, and expected task range. For broad questions "
            "set bounded_task_range min at least 20 and max at most 40; for narrow questions set min "
            "at least 6 and max at most 12."
        )
    if role_label == "semantic reviewer":
        return (
            "You are the Codex DeepResearch independent semantic fit reviewer. Read the JSON request "
            "from stdin. Return only JSON matching the provided schema. Compare the semantic plan only "
            "against the locked expectation oracle and original question. Fail plans that use keyword/"
            "template substitution, drift to another domain, omit source-quality/modality/geography/time/"
            "deliverable constraints, reverse-fit to a generated plan, or have non-executable bounded "
            "tasks. Require all req_001 through req_005 non-negotiable requirements to be covered by "
            "valid angle ids and task ids. Require broad plans to have 5-8 angles, 20-40 bounded tasks, "
            "at least two tasks per angle, and no task query that merely copies an angle title. Require "
            "every bounded task to have max_sources 1-5 and visual tasks to have max_images 1-3 with "
            "concrete visual targets. Return semantic_fit_score >=9 and verdict pass only when there are "
            "no blockers and non_negotiable_coverage_complete is true."
        )
    raise SemanticPlannerAdapterUnavailable(f"Unknown semantic adapter role: {role_label}")


def _semantic_adapter_env_flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _semantic_oracle_raw_request(
    *,
    run_id: str,
    question: str,
    user_constraints: Sequence[str],
    depth_preset: str,
    visual_preference: str | None,
    budget_cap: Mapping[str, Any],
    provided_sources: Sequence[Mapping[str, Any]],
    provided_images: Sequence[Mapping[str, Any]],
    created_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
        "artifact_type": "semantic_oracle_raw_request",
        "run_id": run_id,
        "created_at": created_at,
        "oracle_adapter": CODEX_SEMANTIC_ORACLE_ADAPTER_NAME,
        "prompt_version": CODEX_SEMANTIC_ORACLE_PROMPT_VERSION,
        "original_question": question,
        "user_constraints": [str(item) for item in user_constraints],
        "depth_preset": depth_preset,
        "visual_preference": visual_preference or "auto",
        "budget_cap": dict(budget_cap),
        "provided_sources": [dict(item) for item in provided_sources],
        "provided_images": [dict(item) for item in provided_images],
        "oracle_instructions": [
            "Create an expectation oracle from only the original user inputs.",
            "Use only the listed question, constraints, budget, source, and image inputs.",
            "Capture every explicit requirement and justified inferred constraint.",
        ],
        "response_schema_shape": {
            "oracle_requirement_map": "list",
            "question_scope": "broad|narrow",
            "bounded_task_range": "object",
            "expected_entities": "list",
            "expected_constraints": "list",
            "expected_modalities": "list",
            "required_angles": "list",
            "forbidden_angles": "list",
            "forbidden_internal_implementation_terms": "list",
            "expected_report_shape": "list",
            "language": "string",
        },
    }


def _semantic_oracle_required_fields() -> tuple[str, ...]:
    return (
        "oracle_requirement_map",
        "question_scope",
        "bounded_task_range",
        "expected_entities",
        "expected_constraints",
        "expected_modalities",
        "required_angles",
        "forbidden_angles",
        "forbidden_internal_implementation_terms",
        "expected_report_shape",
        "language",
    )


def _deterministic_oracle_raw_response(
    *,
    request: Mapping[str, Any],
    raw_request_hash: str,
    unavailable_reason: str | None,
) -> dict[str, Any]:
    oracle = _deterministic_expectation_oracle_from_request(request)
    return {
        "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
        "artifact_type": "semantic_oracle_raw_response",
        "oracle_adapter": "deterministic_semantic_oracle_fixture_non_release",
        "prompt_version": "deterministic-p3-sp3-oracle-fixture",
        "semantic_release_eligible": False,
        "expectation_oracle": oracle,
        "provenance": {
            "oracle_adapter": "deterministic_semantic_oracle_fixture_non_release",
            "prompt_version": "deterministic-p3-sp3-oracle-fixture",
            "model_or_surface": "local-deterministic-non-release-fixture",
            "child_session_id": None,
            "session_id": None,
            "session_id_unavailable_reason": (
                unavailable_reason
                or "No independent Codex oracle adapter was configured."
            ),
            "raw_request_hash": raw_request_hash,
            "non_release_fixture": True,
            "adapter_invocation_kind": "local_deterministic_fixture",
        },
    }


def _structured_semantic_oracle_response(
    *,
    raw_request: Mapping[str, Any],
    raw_request_hash: str,
    adapter_response: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(adapter_response, Mapping):
        raise SemanticPlannerAdapterUnavailable("semantic oracle response is not an object")
    raw_response = dict(adapter_response)
    raw_response.setdefault("schema_version", SEMANTIC_PLANNER_SCHEMA_VERSION)
    raw_response.setdefault("artifact_type", "semantic_oracle_raw_response")
    raw_response.setdefault("oracle_adapter", CODEX_SEMANTIC_ORACLE_ADAPTER_NAME)
    raw_response.setdefault("prompt_version", CODEX_SEMANTIC_ORACLE_PROMPT_VERSION)
    raw_response.setdefault("semantic_release_eligible", False)
    provenance = _validate_codex_semantic_role_provenance(
        raw_request_hash=raw_request_hash,
        provenance=raw_response.get("provenance"),
        role_label="semantic oracle",
    )
    raw_response["provenance"] = provenance
    if not isinstance(raw_response.get("expectation_oracle"), Mapping):
        oracle_fields = {
            key: raw_response.get(key)
            for key in _semantic_oracle_required_fields()
            if key in raw_response
        }
        if oracle_fields:
            raw_response["expectation_oracle"] = oracle_fields
    if not isinstance(raw_response.get("expectation_oracle"), Mapping):
        raise SemanticPlannerAdapterUnavailable(
            "semantic oracle adapter response is missing expectation_oracle"
        )
    return raw_response


def _expectation_oracle_from_raw_response(
    *,
    run_path: Path,
    question: str,
    raw_request_path: Path,
    raw_response_path: Path,
    raw_request_content_hash: str,
    raw_request_artifact_hash: str,
    raw_response_hash: str,
    raw_response: Mapping[str, Any],
    timestamp: str,
) -> dict[str, Any]:
    raw_oracle = raw_response.get("expectation_oracle")
    oracle = dict(raw_oracle) if isinstance(raw_oracle, Mapping) else {}
    fallback = _deterministic_expectation_oracle_from_request(
        {
            "original_question": question,
            "user_constraints": [],
            "depth_preset": "standard",
            "visual_preference": "auto",
            "budget_cap": {},
            "provided_sources": [],
            "provided_images": [],
        }
    )
    for key, value in fallback.items():
        oracle.setdefault(key, value)
    oracle["schema_version"] = SEMANTIC_PLANNER_SCHEMA_VERSION
    oracle["artifact_type"] = "semantic_expectation_oracle"
    oracle["run_id"] = run_path.name
    oracle["created_at"] = timestamp
    oracle["locked_at"] = timestamp
    oracle["generated_before_plan_timestamp"] = timestamp
    oracle["semantic_release_eligible"] = False
    oracle["plan_visible_to_oracle"] = False
    oracle["used_production_planner_output"] = False
    oracle["used_hidden_template_class"] = False
    oracle["used_fixed_angle_inventory"] = False
    oracle["no_reverse_fit_fields"] = {
        "plan_visible_to_oracle": False,
        "used_production_planner_output": False,
        "used_hidden_template_class": False,
        "used_fixed_angle_inventory": False,
    }
    provenance = dict(raw_response.get("provenance") or {})
    provenance.setdefault("oracle_adapter", raw_response.get("oracle_adapter"))
    provenance.setdefault("prompt_version", raw_response.get("prompt_version"))
    adapter_raw_request_hash = provenance.get("raw_request_hash")
    provenance["raw_request_path"] = str(raw_request_path)
    provenance["raw_response_path"] = str(raw_response_path)
    provenance["raw_request_content_hash"] = raw_request_content_hash
    provenance["adapter_raw_request_hash"] = adapter_raw_request_hash or raw_request_content_hash
    provenance["raw_request_artifact_hash"] = raw_request_artifact_hash
    provenance["raw_request_hash"] = raw_request_artifact_hash
    provenance["raw_response_artifact_hash"] = raw_response_hash
    provenance["raw_response_hash"] = raw_response_hash
    provenance.setdefault("generated_before_plan_timestamp", timestamp)
    oracle["oracle_provenance"] = provenance
    oracle["provenance"] = provenance
    _apply_manifest_oracle_binding(oracle, raw_response)
    oracle["raw_request_path"] = str(raw_request_path)
    oracle["raw_response_path"] = str(raw_response_path)
    oracle["raw_request_content_hash"] = raw_request_content_hash
    oracle["raw_request_artifact_hash"] = raw_request_artifact_hash
    oracle["raw_request_hash"] = raw_request_artifact_hash
    oracle["raw_response_artifact_hash"] = raw_response_hash
    oracle["raw_response_hash"] = raw_response_hash
    oracle["session_id"] = provenance.get("session_id") or provenance.get("child_session_id")
    oracle["session_id_unavailable_reason"] = provenance.get(
        "session_id_unavailable_reason"
    )
    oracle["oracle_content_hash"] = _oracle_content_hash(oracle)
    oracle["oracle_provenance"]["oracle_content_hash"] = oracle["oracle_content_hash"]
    return oracle


def _deterministic_expectation_oracle_from_request(
    request: Mapping[str, Any],
) -> dict[str, Any]:
    question = str(request.get("original_question") or request.get("question") or "")
    user_constraints = _string_list(request.get("user_constraints"))
    visual_preference = str(request.get("visual_preference") or "auto")
    depth_preset = str(request.get("depth_preset") or "standard")
    requirements = _fixture_extract_semantic_requirements(
        question=question,
        user_constraints=user_constraints,
        visual_preference=visual_preference,
    )
    entities = _fixture_extract_domain_entities(question, requirements)
    scope = _fixture_infer_candidate_question_scope(question, requirements)
    requirement_map = _oracle_requirement_records(requirements)
    expected_modalities = ["text"]
    if any(
        str(requirement.get("requirement_type") or "") == "visual_modality"
        for requirement in requirements
    ):
        expected_modalities.append("visual")
    return {
        "oracle_requirement_map": requirement_map,
        "question_scope": scope,
        "bounded_task_range": _bounded_task_range(scope, depth_preset),
        "expected_entities": entities,
        "expected_constraints": [dict(requirement) for requirement in requirements],
        "expected_modalities": expected_modalities,
        "required_angles": _required_angles_for_requirements(question, requirements),
        "forbidden_angles": _forbidden_angles_for_question(question),
        "forbidden_internal_implementation_terms": _forbidden_internal_terms(),
        "expected_report_shape": _expected_report_shape(requirements),
        "language": _fixture_question_language(question),
        "oracle_requirement_count": len(requirement_map),
        "oracle_generation_basis": {
            "inputs": [
                "original_question",
                "user_constraints",
                "depth_preset",
                "visual_preference",
                "budget_cap",
                "provided_sources",
                "provided_images",
            ],
            "excluded_inputs": [
                "planner_output",
                "class_template_inventory",
                "generated_angles",
                "generated_tasks",
            ],
        },
    }


def _oracle_requirement_records(
    requirements: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, requirement in enumerate(requirements, start=1):
        requirement_type = str(requirement.get("requirement_type") or "requirement")
        records.append(
            {
                "requirement_id": str(requirement.get("requirement_id") or f"req_{index:03d}"),
                "prompt_span": requirement.get("prompt_span"),
                "prompt_text": requirement.get("prompt_text"),
                "requirement_text": requirement.get("requirement_text"),
                "requirement_type": requirement_type,
                "expected_entities": _string_list(requirement.get("expected_entities")),
                "expected_modalities": _expected_modalities_for_requirement(requirement_type),
                "source_quality_constraints": _source_quality_constraints(requirement),
                "geography_constraints": _constraint_text_if_type(requirement, "geography"),
                "time_constraints": _constraint_text_if_type(requirement, "time_range"),
                "output_shape_constraints": _constraint_text_if_type(requirement, "deliverable_shape"),
                "expected_coverage": "full",
                "explicit": bool(requirement.get("explicit")),
                "inferred": not bool(requirement.get("explicit")),
                "inferred_reason": requirement.get("inferred_reason"),
                "non_negotiable": bool(requirement.get("non_negotiable")),
            }
        )
    return records


def _expected_modalities_for_requirement(requirement_type: str) -> list[str]:
    if requirement_type == "visual_modality":
        return ["visual"]
    return ["text"]


def _source_quality_constraints(requirement: Mapping[str, Any]) -> list[str]:
    if str(requirement.get("requirement_type") or "") != "source_quality":
        return []
    return ["official", "regulatory", "primary"]


def _constraint_text_if_type(requirement: Mapping[str, Any], requirement_type: str) -> list[str]:
    if str(requirement.get("requirement_type") or "") != requirement_type:
        return []
    return [str(requirement.get("prompt_text") or requirement.get("requirement_text") or "")]


def _bounded_task_range(scope: str, depth_preset: str) -> dict[str, Any]:
    if scope == "broad":
        if depth_preset == "deep":
            return {"min": 40, "max": 80, "depth_preset": depth_preset}
        if depth_preset == "exhaustive":
            return {"min": 80, "max": 100, "depth_preset": depth_preset}
        return {"min": 20, "max": 40, "depth_preset": depth_preset}
    return {"min": 6, "max": 12, "depth_preset": depth_preset}


def _required_angles_for_requirements(
    question: str,
    requirements: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    subject = _fixture_subject_phrase(
        question,
        _fixture_extract_domain_entities(question, requirements),
    )
    records = [{"angle_requirement": "source baseline", "subject": subject}]
    for requirement in requirements:
        requirement_type = str(requirement.get("requirement_type") or "")
        if requirement_type == "visual_modality":
            records.append({"angle_requirement": "visual evidence", "subject": subject})
        elif requirement_type == "source_quality":
            records.append({"angle_requirement": "official/regulatory sources", "subject": subject})
        elif requirement_type == "time_range":
            records.append({"angle_requirement": "current/recent evidence", "subject": subject})
        elif requirement_type == "geography":
            records.append({"angle_requirement": "requested geography", "subject": subject})
        elif requirement_type == "safety_risk":
            records.append({"angle_requirement": "safety risk/failure modes", "subject": subject})
        elif requirement_type == "deliverable_shape":
            records.append({"angle_requirement": "requested report shape", "subject": subject})
    records.append({"angle_requirement": "counter-evidence and caveats", "subject": subject})
    return records


def _forbidden_angles_for_question(question: str) -> list[str]:
    if _fixture_is_software_implementation_question(question, []):
        return ["generic market scan", "unrequested policy/legal detour"]
    return [
        "Codex runner architecture",
        "semantic planner implementation",
        "local fixture/template inventory",
        "generic report wrapper",
    ]


def _forbidden_internal_terms() -> list[str]:
    return [
        "technical_api",
        "product_market",
        "visual_style",
        "policy_risk",
        "implementation_architecture",
        "heuristic_template_planner",
        "local deterministic template",
        "fixture_semantic_candidate_response_for_validation_tests",
        "budget_cap",
    ]


def _expected_report_shape(requirements: Sequence[Mapping[str, Any]]) -> list[str]:
    shapes = []
    for requirement in requirements:
        if str(requirement.get("requirement_type") or "") == "deliverable_shape":
            text = str(requirement.get("prompt_text") or requirement.get("requirement_text") or "")
            shapes.append(text or "requested synthesized deliverable")
    return shapes or ["source-backed synthesized report"]


def _oracle_content_hash(oracle: Mapping[str, Any]) -> str:
    payload = {
        key: value
        for key, value in dict(oracle).items()
        if key not in {"oracle_content_hash", "created_at", "locked_at"}
    }
    return _sha256_text(json.dumps(payload, sort_keys=True, ensure_ascii=True))


def _structured_semantic_reviewer_response(
    *,
    raw_request: Mapping[str, Any],
    raw_request_hash: str,
    adapter_response: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(adapter_response, Mapping):
        raise SemanticPlannerAdapterUnavailable("semantic reviewer response is not an object")
    raw_response = dict(adapter_response)
    raw_response.setdefault("schema_version", SEMANTIC_PLANNER_SCHEMA_VERSION)
    raw_response.setdefault("artifact_type", "semantic_reviewer_raw_response")
    raw_response.setdefault("reviewer_adapter", CODEX_SEMANTIC_REVIEWER_ADAPTER_NAME)
    raw_response.setdefault("prompt_version", CODEX_SEMANTIC_REVIEWER_PROMPT_VERSION)
    raw_response.setdefault("semantic_release_eligible", False)
    raw_response["provenance"] = _validate_codex_semantic_role_provenance(
        raw_request_hash=raw_request_hash,
        provenance=raw_response.get("provenance"),
        role_label="semantic reviewer",
    )
    if not isinstance(raw_response.get("semantic_plan_review"), Mapping):
        review_fields = {
            key: raw_response.get(key)
            for key in (
                "semantic_fit_score",
                "score_dimensions",
                "prd_score_dimensions",
                "blockers",
                "warnings",
                "substitute_implementation_check",
                "verdict",
                "final_verdict",
            )
            if key in raw_response
        }
        if review_fields:
            raw_response["semantic_plan_review"] = review_fields
    if not isinstance(raw_response.get("semantic_plan_review"), Mapping):
        raise SemanticPlannerAdapterUnavailable(
            "semantic reviewer adapter response is missing semantic_plan_review"
        )
    return raw_response


def _deterministic_review_raw_response(
    *,
    request: Mapping[str, Any],
    plan: SemanticPlan,
    oracle: Mapping[str, Any],
    raw_request_hash: str,
    unavailable_reason: str | None,
) -> dict[str, Any]:
    review = _deterministic_semantic_review(
        question=str(request.get("original_question") or ""),
        plan=plan,
        oracle=oracle,
        adapter_available=False,
        adapter_unavailable_reason=unavailable_reason,
    )
    return {
        "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
        "artifact_type": "semantic_reviewer_raw_response",
        "reviewer_adapter": "deterministic_semantic_reviewer_fixture_non_release",
        "prompt_version": "deterministic-p3-sp3-review-fixture",
        "semantic_release_eligible": False,
        "semantic_plan_review": review,
        "provenance": {
            "reviewer_adapter": "deterministic_semantic_reviewer_fixture_non_release",
            "prompt_version": "deterministic-p3-sp3-review-fixture",
            "model_or_surface": "local-deterministic-non-release-fixture",
            "child_session_id": None,
            "session_id": None,
            "session_id_unavailable_reason": (
                unavailable_reason
                or "No independent Codex reviewer adapter was configured."
            ),
            "raw_request_hash": raw_request_hash,
            "non_release_fixture": True,
            "adapter_invocation_kind": "local_deterministic_fixture",
        },
    }


def _semantic_plan_review_from_raw_response(
    *,
    run_path: Path,
    question: str,
    plan: SemanticPlan,
    oracle: Mapping[str, Any],
    raw_request_path: Path,
    raw_response_path: Path,
    raw_request_content_hash: str,
    raw_request_artifact_hash: str,
    raw_response_hash: str,
    raw_response: Mapping[str, Any],
    timestamp: str,
) -> dict[str, Any]:
    raw_review = raw_response.get("semantic_plan_review")
    adapter_review = dict(raw_review) if isinstance(raw_review, Mapping) else {}
    deterministic = _deterministic_semantic_review(
        question=question,
        plan=plan,
        oracle=oracle,
        adapter_available=not _provenance_is_non_release_fixture(raw_response.get("provenance")),
        adapter_unavailable_reason=None,
    )
    review = {**deterministic, **adapter_review}
    review["schema_version"] = SEMANTIC_PLANNER_SCHEMA_VERSION
    review["artifact_type"] = "semantic_plan_review"
    review["run_id"] = run_path.name
    review["created_at"] = timestamp
    review["planner_mode"] = plan.planner_mode
    review["semantic_release_eligible"] = False
    review["semantic_expectation_oracle_path"] = SEMANTIC_EXPECTATION_ORACLE_FILENAME
    review["semantic_expectation_oracle_hash"] = _sha256_file(
        run_path / SEMANTIC_EXPECTATION_ORACLE_FILENAME
    )
    review["semantic_plan_candidate_hash"] = raw_response.get(
        "semantic_plan_candidate_hash"
    ) or _read_review_request_plan_hash(raw_request_path)
    review["oracle_hash"] = review["semantic_expectation_oracle_hash"]
    review["oracle_content_hash"] = oracle.get("oracle_content_hash")
    review["reviewer_raw_request_path"] = str(raw_request_path)
    review["reviewer_raw_response_path"] = str(raw_response_path)
    review["reviewer_raw_request_content_hash"] = raw_request_content_hash
    review["reviewer_raw_request_artifact_hash"] = raw_request_artifact_hash
    review["reviewer_raw_request_hash"] = raw_request_artifact_hash
    review["reviewer_raw_response_artifact_hash"] = raw_response_hash
    review["reviewer_raw_response_hash"] = raw_response_hash
    review["raw_request_path"] = str(raw_request_path)
    review["raw_response_path"] = str(raw_response_path)
    review["raw_request_content_hash"] = raw_request_content_hash
    review["raw_request_artifact_hash"] = raw_request_artifact_hash
    review["raw_request_hash"] = raw_request_artifact_hash
    review["raw_response_artifact_hash"] = raw_response_hash
    review["raw_response_hash"] = raw_response_hash
    review["question_scope"] = _question_scope(question, plan)
    review["template_use"] = _template_use(plan)
    review["parsed_response_hash"] = _sha256_text(
        json.dumps(adapter_review or deterministic, sort_keys=True, ensure_ascii=True)
    )
    reviewer_provenance = dict(raw_response.get("provenance") or {})
    reviewer_provenance.setdefault("reviewer_adapter", raw_response.get("reviewer_adapter"))
    reviewer_provenance.setdefault("prompt_version", raw_response.get("prompt_version"))
    reviewer_provenance.setdefault("planner_mode", plan.planner_mode)
    reviewer_provenance.setdefault("planner_source", plan.source)
    reviewer_provenance.setdefault("raw_request_required", True)
    reviewer_provenance.setdefault("raw_response_required", True)
    adapter_raw_request_hash = reviewer_provenance.get("raw_request_hash")
    reviewer_provenance["raw_request_path"] = str(raw_request_path)
    reviewer_provenance["raw_response_path"] = str(raw_response_path)
    reviewer_provenance["raw_request_content_hash"] = raw_request_content_hash
    reviewer_provenance["adapter_raw_request_hash"] = (
        adapter_raw_request_hash or raw_request_content_hash
    )
    reviewer_provenance["raw_request_artifact_hash"] = raw_request_artifact_hash
    reviewer_provenance["raw_request_hash"] = raw_request_artifact_hash
    reviewer_provenance["raw_response_artifact_hash"] = raw_response_hash
    reviewer_provenance["raw_response_hash"] = raw_response_hash
    review["reviewer_provenance"] = reviewer_provenance
    review["provenance"] = reviewer_provenance
    review["session_id"] = reviewer_provenance.get("session_id") or reviewer_provenance.get(
        "child_session_id"
    )
    review["session_id_unavailable_reason"] = reviewer_provenance.get(
        "session_id_unavailable_reason"
    )
    _apply_manifest_oracle_binding(review, raw_response)
    review["reviewer_surface"] = reviewer_provenance.get("model_or_surface")
    review["reviewer_prompt_version"] = reviewer_provenance.get("prompt_version")
    review["reviewer_independence"] = _reviewer_independence_from_artifacts(
        plan=plan,
        oracle=oracle,
        reviewer_provenance=reviewer_provenance,
    )
    review["substitute_implementation_check"] = _semantic_substitute_implementation_check(
        plan=plan,
        oracle=oracle,
    )
    review["non_negotiable_coverage_complete"] = _non_negotiable_coverage_complete(
        plan=plan,
        oracle=oracle,
    )
    blockers = _merge_blockers(
        deterministic.get("blockers"),
        adapter_review.get("blockers"),
    )
    if review["reviewer_independence"].get("independent") is not True:
        blockers.append(
            {
                "code": "reviewer_or_oracle_provenance_not_independent",
                "message": "Oracle, planner, and reviewer provenance must be independent.",
            }
        )
    if review["substitute_implementation_check"].get("passed") is not True:
        blockers.append(
            {
                "code": "substitute_implementation_check_failed",
                "message": "Plan appears to substitute internal implementation or generic template work.",
            }
        )
    if review["non_negotiable_coverage_complete"] is not True:
        blockers.append(
            {
                "code": "non_negotiable_coverage_incomplete",
                "message": "Every non-negotiable oracle requirement must be covered or explicitly user-waived.",
            }
        )
    if _provenance_is_non_release_fixture(reviewer_provenance):
        blockers.append(
            {
                "code": "non_release_reviewer_fixture",
                "message": "Local deterministic reviewer fixtures cannot make a plan release-eligible.",
            }
        )
    if _provenance_is_non_release_fixture(oracle.get("oracle_provenance")):
        blockers.append(
            {
                "code": "non_release_oracle_fixture",
                "message": "Local deterministic oracle fixtures cannot make a plan release-eligible.",
            }
        )
    review["blockers"] = _dedupe_blockers(blockers)
    warnings = []
    if isinstance(deterministic.get("warnings"), list):
        warnings.extend(deterministic["warnings"])
    if isinstance(adapter_review.get("warnings"), list):
        warnings.extend(adapter_review["warnings"])
    review["warnings"] = warnings
    score = _numeric_score(review.get("semantic_fit_score"))
    if score is None:
        score = _numeric_score(deterministic.get("semantic_fit_score"))
    if score is None:
        score = 0.0
    if _has_forbidden_internal_leakage(plan=plan, oracle=oracle):
        score = min(score, 6.0)
    if review["blockers"]:
        score = min(score, 8.9)
    review["semantic_fit_score"] = round(score, 2)
    review["score_dimensions"] = _score_dimensions(review)
    review["prd_score_dimensions"] = review["score_dimensions"]
    verdict = "pass" if semantic_review_release_eligible(review) else "release_ineligible"
    review["verdict"] = verdict
    review["final_verdict"] = verdict
    review["semantic_release_eligible"] = verdict == "pass"
    reviewer_provenance["semantic_release_eligible"] = review["semantic_release_eligible"]
    review["reviewer_provenance"] = reviewer_provenance
    review["provenance"] = reviewer_provenance
    return review


def _validate_codex_semantic_role_provenance(
    *,
    raw_request_hash: str,
    provenance: Any,
    role_label: str,
) -> dict[str, Any]:
    if not isinstance(provenance, Mapping):
        raise SemanticPlannerAdapterUnavailable(f"{role_label} response is missing provenance")
    normalized = dict(provenance)
    if str(normalized.get("adapter_invocation_kind") or "") != CODEX_SEMANTIC_ADAPTER_INVOCATION_KIND:
        raise SemanticPlannerAdapterUnavailable(
            f"{role_label} response must record adapter_invocation_kind={CODEX_SEMANTIC_ADAPTER_INVOCATION_KIND}"
        )
    if str(normalized.get("raw_request_hash") or "") != raw_request_hash:
        raise SemanticPlannerAdapterUnavailable(
            f"{role_label} response provenance does not match the raw request hash"
        )
    adapter_command = normalized.get("adapter_command")
    if adapter_command:
        command_parts = (
            shlex.split(adapter_command)
            if isinstance(adapter_command, str)
            else [str(part) for part in adapter_command]
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
            f"{role_label} response lacks Codex raw response identity"
        )
    return normalized


def _read_review_request_plan_hash(path: Path) -> str | None:
    payload = _read_optional_json(path)
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("semantic_plan_candidate_hash")
    return str(value) if value else None


def _deterministic_semantic_review(
    *,
    question: str,
    plan: SemanticPlan,
    oracle: Mapping[str, Any],
    adapter_available: bool,
    adapter_unavailable_reason: str | None,
) -> dict[str, Any]:
    plan_payload = plan.to_dict()
    candidate_validation = validate_semantic_candidate_plan(
        original_question=question,
        plan=plan_payload,
        visual_preference=_normalized_visual_preference(
            plan.raw_request_payload.get("visual_preference")
            if isinstance(plan.raw_request_payload, Mapping)
            else None
        ),
        provided_images=(
            _list(plan.raw_request_payload.get("provided_images"))
            if isinstance(plan.raw_request_payload, Mapping)
            else []
        ),
    )
    blockers: list[dict[str, Any]] = []
    for failure in _list(candidate_validation.get("failures")):
        if isinstance(failure, Mapping):
            blockers.append(
                {
                    "code": str(failure.get("code") or "candidate_validation_failed"),
                    "message": "Candidate plan failed structural semantic validation.",
                    "details": dict(failure),
                }
            )
    if plan.planner_mode != PLANNER_MODE_CODEX_SEMANTIC:
        blockers.append(
            {
                "code": "release_ineligible_planner_mode",
                "planner_mode": plan.planner_mode,
                "message": "Only codex_semantic plans can pass independent semantic review.",
            }
        )
    if not adapter_available:
        blockers.append(
            {
                "code": "reviewer_adapter_unavailable",
                "message": adapter_unavailable_reason
                or "No independent semantic reviewer adapter was configured.",
            }
        )
    if _oracle_omits_material_requirements(oracle):
        blockers.append({"code": "oracle_requirement_map_missing_or_incomplete"})
    if not _plan_covers_oracle_requirements(plan=plan, oracle=oracle):
        blockers.append({"code": "oracle_requirement_coverage_incomplete"})
    blockers.extend(
        _semantic_review_oracle_semantic_blockers(
            question=question,
            plan=plan,
            oracle=oracle,
        )
    )
    if _has_forbidden_internal_leakage(plan=plan, oracle=oracle):
        blockers.append({"code": "forbidden_internal_implementation_leakage"})
    substitute = _semantic_substitute_implementation_check(plan=plan, oracle=oracle)
    dimensions = {
        "intent_preservation": 9.5 if candidate_validation.get("ok") else 6.0,
        "required_entities_constraints": (
            9.5 if _plan_covers_oracle_requirements(plan=plan, oracle=oracle) else 5.5
        ),
        "angle_relevance_diversity": 9.2 if len(plan.angles) >= 5 or not plan.broad_question else 6.5,
        "modality_visual_routing": 9.5 if _modalities_match_oracle(plan=plan, oracle=oracle) else 5.0,
        "forbidden_drift_avoidance": 9.5 if substitute.get("passed") else 4.0,
        "executable_bounded_tasks": 9.4 if plan.bounded_tasks else 4.0,
    }
    score = min(dimensions.values())
    if blockers:
        score = min(score, 8.9)
    if _has_forbidden_internal_leakage(plan=plan, oracle=oracle):
        score = min(score, 6.0)
    return {
        "semantic_fit_score": round(score, 2),
        "blockers": _dedupe_blockers(blockers),
        "warnings": [],
        "checked_prompt_categories": sorted(
            {
                str(record.get("requirement_type") or "")
                for record in _list(oracle.get("oracle_requirement_map"))
                if isinstance(record, Mapping)
            }
        ),
        "score_dimensions": dimensions,
        "prd_score_dimensions": dimensions,
        "substitute_implementation_check": substitute,
        "non_negotiable_coverage_complete": _non_negotiable_coverage_complete(
            plan=plan,
            oracle=oracle,
        ),
        "verdict": "pass" if not blockers and score >= SEMANTIC_FIT_SCORE_THRESHOLD else "release_ineligible",
        "candidate_validation": candidate_validation,
    }


def _score_dimensions(review: Mapping[str, Any]) -> dict[str, float]:
    raw = review.get("score_dimensions") or review.get("prd_score_dimensions")
    if isinstance(raw, Mapping):
        output = {}
        for key in (
            "intent_preservation",
            "required_entities_constraints",
            "angle_relevance_diversity",
            "modality_visual_routing",
            "forbidden_drift_avoidance",
            "executable_bounded_tasks",
        ):
            value = _numeric_score(raw.get(key))
            output[key] = round(value if value is not None else 0.0, 2)
        return output
    return {
        "intent_preservation": 0.0,
        "required_entities_constraints": 0.0,
        "angle_relevance_diversity": 0.0,
        "modality_visual_routing": 0.0,
        "forbidden_drift_avoidance": 0.0,
        "executable_bounded_tasks": 0.0,
    }


def _numeric_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score):
        return None
    return score


def _merge_blockers(*values: Any) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, Mapping):
                blockers.append(dict(item))
            elif isinstance(item, str):
                blockers.append({"code": item})
    return blockers


def _dedupe_blockers(blockers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for blocker in blockers:
        code = str(blocker.get("code") or json.dumps(dict(blocker), sort_keys=True))
        if code in seen:
            continue
        seen.add(code)
        output.append(dict(blocker))
    return output


def _oracle_omits_material_requirements(oracle: Mapping[str, Any]) -> bool:
    requirement_map = oracle.get("oracle_requirement_map")
    return not isinstance(requirement_map, list) or not requirement_map


def _plan_covers_oracle_requirements(
    *,
    plan: SemanticPlan,
    oracle: Mapping[str, Any],
) -> bool:
    coverage_by_id = {
        str(record.get("requirement_id") or ""): record
        for record in plan.requirement_coverage_map
        if isinstance(record, Mapping)
    }
    for requirement in _list(oracle.get("oracle_requirement_map")):
        if not isinstance(requirement, Mapping):
            return False
        requirement_id = str(requirement.get("requirement_id") or "")
        if not requirement_id:
            return False
        coverage = coverage_by_id.get(requirement_id)
        if not isinstance(coverage, Mapping):
            return False
        if coverage.get("coverage_status") != "covered":
            return False
        if requirement.get("non_negotiable") is True:
            if not _string_list(coverage.get("covered_by_angle_ids")):
                return False
            if not _string_list(coverage.get("covered_by_task_ids")):
                return False
    return True


def _modalities_match_oracle(
    *,
    plan: SemanticPlan,
    oracle: Mapping[str, Any],
) -> bool:
    expected = {str(item) for item in _string_list(oracle.get("expected_modalities"))}
    if "visual" not in expected:
        return True
    return any(angle.route != "text_only" for angle in plan.angles) and any(
        str(task.get("route") or "") != "text_only" for task in plan.bounded_tasks
    )


def _semantic_review_oracle_semantic_blockers(
    *,
    question: str,
    plan: SemanticPlan,
    oracle: Mapping[str, Any],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    visual_preference = _normalized_visual_preference(
        plan.raw_request_payload.get("visual_preference")
        if isinstance(plan.raw_request_payload, Mapping)
        else None
    )
    provided_images = (
        _list(plan.raw_request_payload.get("provided_images"))
        if isinstance(plan.raw_request_payload, Mapping)
        else []
    )
    expected_modalities = _string_list(oracle.get("expected_modalities"))
    angles = [angle.to_dict() for angle in plan.angles]
    tasks = [
        task for task in plan.bounded_tasks if isinstance(task, Mapping)
    ]
    if _visual_optional_without_primary_visual_evidence(
        question=question,
        visual_preference=visual_preference,
        provided_images=provided_images,
        expected_modalities=expected_modalities,
    ):
        profile = _candidate_visual_support_profile(angles=angles, tasks=tasks)
        if profile["visual_required_angle_count"] or profile["visual_required_task_count"]:
            blockers.append(
                {
                    "code": "MODALITY_OPTIONALITY_REVERSED",
                    "message": (
                        "Oracle did not make visual evidence mandatory, but the plan "
                        "made optional visual support required."
                    ),
                    "profile": profile,
                }
            )
        if (
            profile["visual_angle_count"] > profile["max_visual_angles"]
            or profile["visual_task_count"] > profile["max_visual_tasks"]
            or (
                profile["text_task_count"] > 0
                and profile["visual_task_count"] >= profile["text_task_count"]
            )
        ):
            blockers.append(
                {
                    "code": "visual_optional_visual_work_dominates_primary_evidence",
                    "message": (
                        "Oracle-primary text/document/table/structured evidence was "
                        "displaced by optional visual work."
                    ),
                    "profile": profile,
                }
            )

    requirements = [
        requirement
        for requirement in _list(oracle.get("oracle_requirement_map"))
        if isinstance(requirement, Mapping)
    ]
    if any(_candidate_requirement_is_req003_comparison(req) for req in requirements):
        comparison_requirements = [
            req
            for req in requirements
            if _candidate_requirement_is_req003_comparison(req)
        ]
        if not any(
            _candidate_has_comparison_deliverable_task(
                tasks=tasks,
                requirement=req,
            )
            for req in comparison_requirements
        ):
            blockers.append(
                {
                    "code": "REQ_003_COMPARISON_DELIVERABLE_INCOMPLETE",
                    "message": (
                        "Req_003 comparison/output-shape oracle requirements need a "
                        "bounded side-by-side comparison deliverable with status, "
                        "evidence, caveat, and remediation fields."
                    ),
                }
            )
        if any(
            _candidate_requirement_needs_prioritized_remediation(req)
            for req in requirements
        ) and not _candidate_has_prioritized_remediation_task(tasks=tasks):
            blockers.append(
                {
                    "code": "REQ_003_PRIORITIZED_REMEDIATION_MISSING",
                    "message": (
                        "Req_003 asks for remediation, but no bounded prioritized "
                        "remediation task/artifact is present."
                    ),
                }
            )
    normalized_modalities = {
        _normalize_text(modality).replace(" ", "_")
        for modality in expected_modalities
    }
    structured_expected = bool(
        normalized_modalities
        & {
            "structured_model_or_artifact",
            "structured_artifact",
            "structured_model",
        }
    ) or any(
        _candidate_requirement_needs_structured_artifact_assessment(req)
        for req in requirements
    )
    if structured_expected and not _candidate_has_structured_artifact_assessment_task(
        tasks=tasks
    ):
        blockers.append(
            {
                "code": "STRUCTURED_ARTIFACT_ROUTE_INCOMPLETE",
                "message": (
                    "Oracle expects document/table/structured artifact assessment, "
                    "but the plan lacks a bounded text/document structured-artifact "
                    "assessment task."
                ),
            }
        )
    return _dedupe_blockers(blockers)


def _non_negotiable_coverage_complete(
    *,
    plan: SemanticPlan,
    oracle: Mapping[str, Any],
) -> bool:
    return _plan_covers_oracle_requirements(plan=plan, oracle=oracle)


def _has_forbidden_internal_leakage(
    *,
    plan: SemanticPlan,
    oracle: Mapping[str, Any],
) -> bool:
    return bool(_forbidden_internal_leakage_terms(plan=plan, oracle=oracle))


def _semantic_substitute_implementation_check(
    *,
    plan: SemanticPlan,
    oracle: Mapping[str, Any],
) -> dict[str, Any]:
    records = _plan_executable_review_records(plan)
    text = json.dumps([value for _field_name, value in records], ensure_ascii=False).lower()
    forbidden_angles = [item.lower() for item in _string_list(oracle.get("forbidden_angles"))]
    leakage_terms = _forbidden_internal_leakage_terms(plan=plan, oracle=oracle)
    forbidden_angle_hits = [
        term
        for term in forbidden_angles
        if term and _forbidden_angle_term_matches(term=term, records=records)
    ]
    generic_hits = [
        phrase
        for phrase in (
            "generic report wrapper",
            "local deterministic template",
            "primary source discovery only",
        )
        if phrase in text
    ]
    passed = not leakage_terms and not forbidden_angle_hits and not generic_hits
    return {
        "passed": passed,
        "checked": True,
        "forbidden_internal_implementation_terms_found": leakage_terms,
        "forbidden_angle_terms_found": forbidden_angle_hits,
        "generic_wrapper_terms_found": generic_hits,
        "blocked_reason": None if passed else "substitute_or_forbidden_implementation_detected",
    }


def _forbidden_angle_term_matches(
    *,
    term: str,
    records: Sequence[tuple[str, str]],
) -> bool:
    normalized_term = _normalize_text(term)
    if not normalized_term:
        return False
    for field_name, value in records:
        normalized_value = _normalize_text(value)
        if not _contains_normalized_phrase(normalized_value, normalized_term):
            continue
        if _forbidden_angle_reference_is_negative_scope(
            field_name=field_name,
            normalized_term=normalized_term,
            normalized_value=normalized_value,
        ):
            continue
        return True
    return False


def _forbidden_angle_reference_is_negative_scope(
    *,
    field_name: str,
    normalized_term: str,
    normalized_value: str,
) -> bool:
    if any(
        marker in field_name
        for marker in ("excluded_scope", "negative_scope")
    ):
        return True
    negative_patterns = (
        rf"\b(?:no|not|exclude|excluded|excludes|excluding|avoid|avoids|avoided|without)\b"
        rf"(?:\s+\w+){{0,6}}\s+{re.escape(normalized_term)}\b",
        rf"\b{re.escape(normalized_term)}\b(?:\s+\w+){{0,6}}\s+"
        rf"(?:excluded|out of scope|not in scope|not part of scope)\b",
        rf"\bdo\s+not\b(?:\s+\w+){{0,6}}\s+{re.escape(normalized_term)}\b",
        rf"\b(?:rather\s+than|instead\s+of)\b(?:\s+\w+){{0,6}}\s+{re.escape(normalized_term)}\b",
    )
    return any(re.search(pattern, normalized_value) for pattern in negative_patterns)


def _forbidden_internal_leakage_terms(
    *,
    plan: SemanticPlan,
    oracle: Mapping[str, Any],
) -> list[str]:
    forbidden = _string_list(oracle.get("forbidden_internal_implementation_terms"))
    if not forbidden:
        forbidden = _forbidden_internal_terms()
    records = _plan_executable_review_records(plan)
    return [
        term
        for term in _ordered_unique(forbidden)
        if _forbidden_internal_term_matches(term=term, records=records)
    ]


def _forbidden_internal_term_matches(
    *,
    term: str,
    records: Sequence[tuple[str, str]],
) -> bool:
    normalized_term = _normalize_text(term)
    if not normalized_term:
        return False
    compact_term = normalized_term.replace(" ", "_")
    for _field_name, value in records:
        normalized_value = _normalize_text(value)
        if not _contains_normalized_phrase(normalized_value, normalized_term):
            continue
        if compact_term == "schema":
            if _schema_term_is_internal_leakage(normalized_value):
                return True
            continue
        if compact_term in {"planner", "adapter", "oracle", "budget"}:
            if _ambiguous_internal_term_has_context(compact_term, normalized_value):
                return True
            continue
        if compact_term == "architecture":
            if _architecture_term_is_internal_leakage(normalized_value):
                return True
            continue
        if compact_term == "model":
            if _model_term_is_internal_leakage(normalized_value):
                return True
            continue
        if compact_term in {"test", "testing"}:
            if _testing_term_is_internal_leakage(normalized_value):
                return True
            continue
        return True
    return False


def _contains_normalized_phrase(text: str, phrase: str) -> bool:
    if not text or not phrase:
        return False
    return f" {phrase} " in f" {text} "


def _schema_term_is_internal_leakage(normalized_value: str) -> bool:
    internal_schema_phrases = (
        "adapter schema",
        "schema adapter",
        "schema contract",
        "contract schema",
        "planner schema",
        "schema planner",
        "oracle schema",
        "schema oracle",
        "subagent schema",
        "schema subagent",
        "run id schema",
        "requirement id schema",
        "internal schema",
        "implementation schema",
        "schema implementation",
        "json schema",
        "output schema",
        "response schema",
        "input schema",
        "schema validation",
        "schema stub",
        "schema stubs",
        "schema file",
    )
    if any(
        _contains_normalized_phrase(normalized_value, phrase)
        for phrase in internal_schema_phrases
    ):
        return True
    internal_terms = {
        "implementation",
        "internal",
        "internals",
        "contract",
        "adapter",
        "planner",
        "oracle",
        "subagent",
        "run",
        "id",
        "requirement",
        "deterministic",
        "fixture",
        "template",
        "local",
        "raw",
        "provenance",
        "command",
        "json",
    }
    if _normalized_text_has_any_token(normalized_value, internal_terms):
        return True
    deliverable_terms = {
        "comparison",
        "comparative",
        "table",
        "matrix",
        "report",
        "deliverable",
        "shape",
        "structure",
        "format",
        "column",
        "columns",
        "field",
        "fields",
        "row",
        "rows",
        "taxonomy",
        "outline",
    }
    if _normalized_text_has_any_token(normalized_value, deliverable_terms):
        return False
    return True


def _architecture_term_is_internal_leakage(normalized_value: str) -> bool:
    software_terms = {
        "adapter",
        "api",
        "backend",
        "code",
        "codex",
        "component",
        "data",
        "database",
        "deployment",
        "frontend",
        "implementation",
        "infrastructure",
        "internal",
        "internals",
        "module",
        "pipeline",
        "planner",
        "repository",
        "runner",
        "runtime",
        "sdk",
        "semantic",
        "service",
        "software",
        "system",
        "technical",
        "test",
    }
    physical_terms = {
        "building",
        "campus",
        "clinical",
        "construction",
        "facility",
        "floor",
        "healthcare",
        "hospital",
        "layout",
        "medical",
        "patient",
        "physical",
        "room",
        "site",
        "spatial",
        "ward",
        "건축",
        "공간",
        "병원",
        "시설",
    }
    tokens = set(normalized_value.split())
    if tokens & physical_terms:
        if not (tokens & software_terms):
            return False
        negative_software_architecture = (
            _forbidden_angle_reference_is_negative_scope(
                field_name="architecture_context",
                normalized_term="software architecture",
                normalized_value=normalized_value,
            )
        )
        if negative_software_architecture:
            return False
    if tokens & software_terms:
        return True
    if tokens & physical_terms:
        return False
    return True


def _model_term_is_internal_leakage(normalized_value: str) -> bool:
    software_terms = {
        "adapter",
        "api",
        "backend",
        "code",
        "codex",
        "component",
        "database",
        "frontend",
        "implementation",
        "internal",
        "module",
        "planner",
        "repository",
        "runner",
        "runtime",
        "sdk",
        "semantic",
        "service",
        "software",
        "system",
    }
    non_software_terms = {
        "architectural",
        "building",
        "clinical",
        "construction",
        "evaluation",
        "foundation",
        "hospital",
        "language",
        "learning",
        "machine",
        "physical",
        "policy",
        "public",
        "safety",
        "scale",
        "statistical",
        "건축",
        "모델",
        "안전",
        "평가",
    }
    tokens = set(normalized_value.split())
    if tokens & non_software_terms and not (tokens & software_terms):
        return False
    return bool(tokens & software_terms)


def _testing_term_is_internal_leakage(normalized_value: str) -> bool:
    software_terms = {
        "adapter",
        "api",
        "automation",
        "backend",
        "code",
        "codex",
        "component",
        "e2e",
        "implementation",
        "internal",
        "module",
        "pipeline",
        "planner",
        "repository",
        "runner",
        "runtime",
        "sdk",
        "semantic",
        "software",
        "unit",
    }
    non_software_terms = {
        "clinical",
        "crash",
        "fire",
        "hospital",
        "laboratory",
        "material",
        "medical",
        "physical",
        "public",
        "safety",
        "standard",
        "standards",
        "안전",
        "시험",
        "테스트",
        "평가",
    }
    tokens = set(normalized_value.split())
    if tokens & non_software_terms and not (tokens & software_terms):
        return False
    return bool(tokens & software_terms)


def _ambiguous_internal_term_has_context(term: str, normalized_value: str) -> bool:
    context_terms_by_term = {
        "planner": {
            "implementation",
            "internal",
            "internals",
            "deterministic",
            "fixture",
            "template",
            "local",
            "adapter",
            "oracle",
            "subagent",
            "schema",
            "contract",
            "raw",
            "output",
            "provenance",
            "command",
        },
        "adapter": {
            "implementation",
            "internal",
            "internals",
            "deterministic",
            "fixture",
            "template",
            "local",
            "planner",
            "oracle",
            "subagent",
            "schema",
            "contract",
            "raw",
            "output",
            "provenance",
            "command",
        },
        "oracle": {
            "implementation",
            "internal",
            "internals",
            "deterministic",
            "fixture",
            "template",
            "local",
            "planner",
            "adapter",
            "subagent",
            "schema",
            "contract",
            "raw",
            "output",
            "provenance",
            "command",
            "requirement",
        },
        "budget": {
            "implementation",
            "internal",
            "internals",
            "deterministic",
            "fixture",
            "template",
            "local",
            "planner",
            "adapter",
            "oracle",
            "subagent",
            "token",
            "context",
            "command",
        },
    }
    phrases_by_term = {
        "planner": (
            "semantic planner implementation",
            "planner implementation",
            "planner internals",
            "planner output",
            "planner adapter",
            "planner schema",
            "heuristic template planner",
        ),
        "adapter": (
            "adapter implementation",
            "adapter internals",
            "adapter schema",
            "adapter contract",
            "adapter command",
            "local adapter",
        ),
        "oracle": (
            "oracle implementation",
            "oracle internals",
            "oracle schema",
            "oracle contract",
            "oracle requirement",
            "oracle adapter",
        ),
        "budget": (
            "token budget",
            "context budget",
            "planner budget",
            "adapter budget",
            "oracle budget",
            "subagent budget",
        ),
    }
    if any(
        _contains_normalized_phrase(normalized_value, phrase)
        for phrase in phrases_by_term.get(term, ())
    ):
        return True
    return _normalized_text_has_any_token(
        normalized_value,
        context_terms_by_term.get(term, set()),
    )


def _normalized_text_has_any_token(text: str, tokens: set[str]) -> bool:
    padded = f" {text} "
    return any(f" {token} " in padded for token in tokens)


def _plan_executable_review_records(plan: SemanticPlan) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []

    def add_value(field_name: str, value: Any) -> None:
        if isinstance(value, str):
            if value.strip():
                records.append((field_name, value))
            return
        if isinstance(value, Mapping):
            for nested_value in value.values():
                add_value(field_name, nested_value)
            return
        if isinstance(value, (list, tuple, set)):
            for nested_value in value:
                add_value(field_name, nested_value)

    for field_name, value in (
        ("intent_summary", plan.intent_summary),
        ("domain_entities", plan.domain_entities),
        ("constraints", plan.constraints),
    ):
        add_value(field_name, value)
    for angle in plan.angles:
        for field_name, value in (
            ("angles.title", angle.title),
            ("angles.research_question", angle.research_question),
            ("angles.why_this_angle_matters", angle.why_this_angle_matters),
            ("angles.included_scope", angle.included_scope),
            ("angles.expected_source_types", angle.expected_source_types),
            ("angles.expected_visual_targets", angle.expected_visual_targets),
            ("angles.expected_artifacts", angle.expected_artifacts),
            ("angles.search_queries", angle.search_queries),
            ("angles.success_criteria", angle.success_criteria),
            ("angles.report_section", angle.report_section),
            ("angles.risk_or_contradiction_checks", angle.risk_or_contradiction_checks),
        ):
            add_value(field_name, value)
    for task in plan.bounded_tasks:
        if not isinstance(task, Mapping):
            continue
        for field_name in (
            "query",
            "source_policy",
            "expected_source_types",
            "expected_visual_targets",
            "expected_artifacts",
            "success_criteria",
            "done_condition",
        ):
            add_value(f"bounded_tasks.{field_name}", task.get(field_name))
    return records


def _plan_executable_review_text(plan: SemanticPlan) -> str:
    values = [value for _field_name, value in _plan_executable_review_records(plan)]
    return json.dumps(values, ensure_ascii=False, sort_keys=True).lower()


def _reviewer_independence_from_artifacts(
    *,
    plan: SemanticPlan,
    oracle: Mapping[str, Any],
    reviewer_provenance: Mapping[str, Any],
) -> dict[str, Any]:
    planner = dict(plan.planner_provenance or {})
    oracle_provenance = dict(oracle.get("oracle_provenance") or oracle.get("provenance") or {})
    reverse_fit = {
        "plan_visible_to_oracle": oracle.get("plan_visible_to_oracle"),
        "used_production_planner_output": oracle.get("used_production_planner_output"),
        "used_hidden_template_class": oracle.get("used_hidden_template_class"),
        "used_fixed_angle_inventory": oracle.get("used_fixed_angle_inventory"),
    }
    planner_ids = _provenance_identity_set(planner)
    oracle_ids = _provenance_identity_set(oracle_provenance)
    reviewer_ids = _provenance_identity_set(reviewer_provenance)
    oracle_planner_shared = bool(planner_ids & oracle_ids)
    reviewer_planner_shared = bool(planner_ids & reviewer_ids)
    reviewer_oracle_shared = bool(oracle_ids & reviewer_ids)
    missing = []
    for label, provenance in (
        ("planner", planner),
        ("oracle", oracle_provenance),
        ("reviewer", reviewer_provenance),
    ):
        if not _provenance_has_required_raw_artifacts(provenance):
            missing.append(label)
    reverse_fit_detected = any(value is not False for value in reverse_fit.values())
    independent = (
        plan.planner_mode == PLANNER_MODE_CODEX_SEMANTIC
        and not missing
        and not oracle_planner_shared
        and not reviewer_planner_shared
        and not reviewer_oracle_shared
        and not reverse_fit_detected
        and not _provenance_is_non_release_fixture(oracle_provenance)
        and not _provenance_is_non_release_fixture(reviewer_provenance)
    )
    return {
        "independent": independent,
        "oracle_planner_shared_provenance": oracle_planner_shared,
        "reviewer_planner_shared_provenance": reviewer_planner_shared,
        "reviewer_oracle_shared_provenance": reviewer_oracle_shared,
        "missing_required_provenance": missing,
        "reverse_fit_detected": reverse_fit_detected,
        "reverse_fit_fields": reverse_fit,
        "planner_identity": sorted(planner_ids),
        "oracle_identity": sorted(oracle_ids),
        "reviewer_identity": sorted(reviewer_ids),
        "status": "passed" if independent else "failed",
    }


def _provenance_identity_set(provenance: Mapping[str, Any]) -> set[str]:
    identities = set()
    for field in (
        "child_session_id",
        "session_id",
        "raw_response_id",
        "response_id",
        "adapter_invocation_id",
    ):
        value = provenance.get(field)
        if isinstance(value, str) and value.strip():
            identities.add(f"{field}:{value.strip()}")
    event_identity = _qualified_codex_event_identity(provenance)
    if event_identity:
        identities.add(event_identity)
    return identities


def _provenance_release_identity_set(provenance: Mapping[str, Any]) -> set[str]:
    identities = set()
    for field in (
        "child_session_id",
        "session_id",
        "raw_response_id",
        "response_id",
    ):
        value = provenance.get(field)
        if isinstance(value, str) and value.strip():
            identities.add(f"{field}:{value.strip()}")
    event_identity = _qualified_codex_event_identity(provenance)
    if event_identity:
        identities.add(event_identity)
    return identities


def _qualified_codex_event_identity(provenance: Mapping[str, Any]) -> str | None:
    event_id = str(provenance.get("codex_event_id") or "").strip()
    if not event_id:
        return None
    session_id = str(
        provenance.get("session_id")
        or provenance.get("child_session_id")
        or provenance.get("conversation_id")
        or ""
    ).strip()
    if session_id:
        return f"codex_event_id:{session_id}:{event_id}"
    return f"codex_event_id:{event_id}"


def _provenance_has_required_raw_artifacts(provenance: Mapping[str, Any]) -> bool:
    return bool(
        str(provenance.get("raw_request_hash") or "").strip()
        and str(provenance.get("raw_response_hash") or "").strip()
        and _provenance_release_identity_set(provenance)
    )


def _provenance_is_non_release_fixture(provenance: Any) -> bool:
    return isinstance(provenance, Mapping) and (
        provenance.get("non_release_fixture") is True
        or str(provenance.get("adapter_invocation_kind") or "") == "local_deterministic_fixture"
    )


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
            "item",
            "items",
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
        for key in (
            "item",
            "items",
            "response",
            "payload",
            "data",
            "content",
            "message",
            "output",
            "text",
            "json",
        ):
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
    if isinstance(value, Sequence):
        for item in reversed(value):
            payload = _semantic_adapter_payload_from_value(item)
            if payload is not None:
                return payload
    return None


def _is_semantic_adapter_response_payload(value: Mapping[str, Any]) -> bool:
    return (
        "candidate_plan" in value
        or "expectation_oracle" in value
        or "semantic_plan_review" in value
        or value.get("artifact_type") == "semantic_planner_raw_response"
        or value.get("artifact_type") == "semantic_oracle_raw_response"
        or value.get("artifact_type") == "semantic_reviewer_raw_response"
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
            for source in (event, event.get("item")):
                if not isinstance(source, Mapping):
                    continue
                for alias in aliases:
                    value = source.get(alias)
                    if value:
                        provenance[target] = str(value)
                        break
                if target in provenance:
                    break
    if event_types:
        provenance["codex_event_types"] = list(dict.fromkeys(event_types))
    return provenance


def _preview_text(value: str, limit: int = 240) -> str:
    text = " ".join((value or "").split())
    if not text:
        return "<empty>"
    return text[:limit] + ("..." if len(text) > limit else "")


def _candidate_visual_support_profile(
    *,
    angles: Sequence[Mapping[str, Any]],
    tasks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    visual_angles = [
        angle
        for angle in angles
        if str(angle.get("route") or "text_only") != "text_only"
        or bool(_string_list(angle.get("expected_visual_targets")))
    ]
    visual_tasks = [
        task
        for task in tasks
        if _candidate_task_has_visual_support(task)
    ]
    max_visual_angles = (
        max(1, math.floor(len(angles) * VISUAL_OPTIONAL_SUPPORT_MAX_ANGLE_RATIO))
        if angles
        else 0
    )
    max_visual_tasks = (
        max(1, math.floor(len(tasks) * VISUAL_OPTIONAL_SUPPORT_MAX_TASK_RATIO))
        if tasks
        else 0
    )
    return {
        "angle_count": len(angles),
        "task_count": len(tasks),
        "visual_angle_count": len(visual_angles),
        "visual_task_count": len(visual_tasks),
        "text_task_count": max(0, len(tasks) - len(visual_tasks)),
        "visual_required_angle_count": sum(
            1
            for angle in visual_angles
            if str(angle.get("route") or "") == "visual_required"
        ),
        "visual_required_task_count": sum(
            1
            for task in visual_tasks
            if str(task.get("route") or "") == "visual_required"
        ),
        "max_visual_angles": max_visual_angles,
        "max_visual_tasks": max_visual_tasks,
        "visual_angle_ids": [
            str(angle.get("angle_id") or "")
            for angle in visual_angles
            if str(angle.get("angle_id") or "")
        ],
        "visual_task_ids": [
            str(task.get("task_id") or "")
            for task in visual_tasks
            if str(task.get("task_id") or "")
        ],
    }


def _candidate_task_has_visual_support(task: Mapping[str, Any]) -> bool:
    route = str(task.get("route") or "text_only")
    if route != "text_only":
        return True
    if _string_list(task.get("expected_visual_targets")):
        return True
    if _task_expected_evidence(task) & VISUAL_EXPECTED_EVIDENCE:
        return True
    if _semantic_task_expected_visual_artifacts(task):
        return True
    return _nonnegative_int(task.get("max_images")) > 0


def _candidate_requirement_text(requirement: Mapping[str, Any]) -> str:
    return json.dumps(
        [
            requirement.get("requirement_id"),
            requirement.get("requirement_type"),
            requirement.get("prompt_text"),
            requirement.get("requirement_text"),
            requirement.get("output_shape_constraints"),
        ],
        ensure_ascii=False,
        sort_keys=True,
    )


def _candidate_task_deliverable_text(task: Mapping[str, Any]) -> str:
    return json.dumps(
        [
            task.get("query"),
            task.get("route"),
            task.get("expected_source_types"),
            task.get("expected_artifacts"),
            task.get("success_criteria"),
            task.get("done_condition"),
        ],
        ensure_ascii=False,
        sort_keys=True,
    ).lower()


def _candidate_requirement_is_req003_comparison(
    requirement: Mapping[str, Any],
) -> bool:
    requirement_id = str(requirement.get("requirement_id") or "").lower()
    requirement_type = str(requirement.get("requirement_type") or "").lower()
    text = _candidate_requirement_text(requirement).lower()
    if requirement_id != "req_003" and requirement_type != "analysis_comparison_output_shape":
        return False
    normalized_modalities = {
        _normalize_text(item).replace(" ", "_")
        for item in _string_list(requirement.get("expected_modalities"))
    }
    if normalized_modalities & {
        "structured_comparison",
        "comparison_matrix",
        "comparison_table",
    }:
        return True
    if requirement_type == "analysis_comparison_output_shape":
        return True
    output_shape_text = " ".join(
        _string_list(requirement.get("output_shape_constraints"))
    ).lower()
    if _contains_any(
        output_shape_text,
        (
            "side-by-side",
            "comparison table",
            "comparison matrix",
            "matrix",
            "requirements matrix",
            "\uc694\uad6c\uc0ac\ud56d\ubcc4 \ube44\uad50",
            "\ube44\uad50\ud45c",
            "\ube44\uad50 \ub9e4\ud2b8\ub9ad\uc2a4",
            "\ub300\uc751\ud45c",
        ),
    ):
        return True
    if not _contains_any(text, ("compare", "comparison", "\ube44\uad50")):
        return False
    return _contains_any(
        text,
        (
            "side-by-side",
            "matrix",
            "table",
            "\ub300\uc751\ud45c",
            "\ube44\uad50\ud45c",
            "\ucda9\uc871\ub3c4",
            "\uc77c\uce58",
            "\ubd88\uc77c\uce58",
            "\ud310\ub2e8 \ubd88\uac00",
            "\uaca9\ucc28",
            "\ubcf4\uc644 \uc870\uce58",
        ),
    )


def _candidate_has_comparison_deliverable_task(
    *,
    tasks: Sequence[Mapping[str, Any]],
    requirement: Mapping[str, Any],
) -> bool:
    strict_status_fields = _candidate_requirement_needs_compliance_status_fields(
        requirement
    )
    status_groups = (
        ("match", "matched", "meets", "\ub9e4\uce58", "\uc77c\uce58", "\ucda9\uc871"),
        ("partial", "partially", "\ubd80\ubd84", "\ubd80\ubd84 \ucda9\uc871"),
        ("mismatch", "gap", "non-compliant", "\ubd88\uc77c\uce58", "\ubbf8\ucda9\uc871", "\uac04\uadf9"),
        ("unverifiable", "unknown", "not verifiable", "\ubbf8\ud655\uc778", "\ud655\uc778 \ubd88\uac00", "\ubbf8\uc0c1"),
    )
    for task in tasks:
        text = _candidate_task_deliverable_text(task)
        if not _contains_any(
            text,
            (
                "side-by-side",
                "comparison table",
                "comparison matrix",
                "matrix",
                "table",
                "\ub300\uc751\ud45c",
                "\ube44\uad50\ud45c",
                "\ub9e4\ud2b8\ub9ad\uc2a4",
            ),
        ):
            continue
        if not _contains_any(
            text,
            (
                "requirement",
                "criterion",
                "criteria",
                "standard",
                "compared item",
                "policy",
                "guidance",
                "pictogram",
                "visual message",
                "behavior guidance",
                "model output",
                "structured artifact",
                "artifact",
                "public design",
                "tender",
                "\uc694\uad6c\uc0ac\ud56d",
                "\uc694\uad6c",
                "\uae30\uc900",
                "\ud56d\ubaa9",
                "\uaddc\uc815",
                "\uc9c0\uce68",
                "\ud53d\ud1a0\uadf8\ub7a8",
                "\uc2dc\uac01 \uba54\uc2dc\uc9c0",
                "\ud589\ub3d9\uc694\ub839",
                "\ucc28\uc774",
                "\ubaa8\ub378 \uc0b0\ucd9c\ubb3c",
                "\uc0b0\ucd9c\ubb3c",
                "\uacf5\uacf5 \uc124\uacc4",
                "\uc785\ucc30",
            ),
        ):
            continue
        if strict_status_fields:
            if not all(_contains_any(text, group) for group in status_groups):
                continue
        elif not _contains_any(
            text,
            (
                "difference",
                "differences",
                "alignment",
                "mapping",
                "maps",
                "gap",
                "omission",
                "ambiguity",
                "\ucc28\uc774",
                "\ub300\uc870",
                "\ub300\uc751",
                "\uc77c\uce58",
                "\ub204\ub77d",
                "\ubaa8\ud638",
            ),
        ):
            continue
        if not _contains_any(text, ("evidence", "citation", "source", "\uadfc\uac70", "\uc778\uc6a9")):
            continue
        return True
    return False


def _candidate_requirement_needs_compliance_status_fields(
    requirement: Mapping[str, Any],
) -> bool:
    text = _candidate_requirement_text(requirement).lower()
    return _contains_any(
        text,
        (
            "partial",
            "mismatch",
            "unverifiable",
            "compliance",
            "non-compliant",
            "\ubd80\ubd84 \uc77c\uce58",
            "\ubd88\uc77c\uce58",
            "\ud310\ub2e8 \ubd88\uac00",
            "\uc900\uc218",
            "\ucda9\uc871\ub3c4",
        ),
    )


def _candidate_requirement_needs_prioritized_remediation(
    requirement: Mapping[str, Any],
) -> bool:
    text = _candidate_requirement_text(requirement).lower()
    action_requested = _contains_any(
        text,
        (
            "remediation",
            "remediate",
            "recommendation",
            "recommendations",
            "next action",
            "\uac1c\uc120",
            "\ubcf4\uc644",
            "\uc870\uce58",
            "\uad8c\uace0",
        ),
    )
    priority_requested = _contains_any(
        text,
        (
            "prioritized",
            "priority",
            "rank",
            "severity",
            "effort",
            "\uc6b0\uc120\uc21c\uc704",
            "\uc6b0\uc120",
            "\uc21c\uc704",
            "\uc911\ub300\uc131",
        ),
    )
    return action_requested and priority_requested


def _candidate_has_prioritized_remediation_task(
    *,
    tasks: Sequence[Mapping[str, Any]],
) -> bool:
    for task in tasks:
        text = _candidate_task_deliverable_text(task)
        if not _contains_any(
            text,
            (
                "remediation",
                "recommendation",
                "next action",
                "\uac1c\uc120",
                "\ubcf4\uc644",
                "\uc870\uce58",
                "\uad8c\uace0",
            ),
        ):
            continue
        if not _contains_any(text, ("priority", "prioritized", "rank", "severity", "\uc6b0\uc120\uc21c\uc704", "\uc911\ub300\uc131")):
            continue
        if not _contains_any(text, ("impact", "effort", "confidence", "evidence", "\uc601\ud5a5", "\ub178\ub825", "\uc2e0\ub8b0", "\uadfc\uac70")):
            continue
        return True
    return False


def _candidate_requirement_needs_structured_artifact_assessment(
    requirement: Mapping[str, Any],
) -> bool:
    requirement_type = str(requirement.get("requirement_type") or "").lower()
    text = _candidate_requirement_text(requirement).lower()
    if requirement_type in {
        "structured_artifact_assessment",
        "structured_model_or_artifact",
        "structured_model_or_artifact_assessment",
    }:
        return True
    return _contains_any(
        text,
        (
            "structured artifact",
            "model output",
            "model outputs",
            "artifact assessment",
            "bim",
            "\ubaa8\ub378 \uc0b0\ucd9c\ubb3c",
            "\uc0b0\ucd9c\ubb3c",
        ),
    )


def _candidate_has_structured_artifact_assessment_task(
    *,
    tasks: Sequence[Mapping[str, Any]],
) -> bool:
    for task in tasks:
        if str(task.get("route") or "text_only") != "text_only":
            continue
        text = _candidate_task_deliverable_text(task)
        if not _contains_any(
            text,
            (
                "structured artifact",
                "model output",
                "model outputs",
                "artifact",
                "bim",
                "ifc",
                "\ubaa8\ub378",
                "\uc0b0\ucd9c\ubb3c",
            ),
        ):
            continue
        if not _contains_any(
            text,
            (
                "inventory",
                "field",
                "attribute",
                "object",
                "schema",
                "criterion",
                "criteria",
                "requirement",
                "assessment",
                "\uc778\ubca4\ud1a0\ub9ac",
                "\ud544\ub4dc",
                "\uc18d\uc131",
                "\uac1d\uccb4",
                "\uae30\uc900",
                "\uc694\uad6c",
                "\ud3c9\uac00",
            ),
        ):
            continue
        return True
    return False


def validate_semantic_candidate_plan(
    *,
    original_question: str,
    plan: Mapping[str, Any],
    visual_preference: str | None = None,
    provided_images: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Validate P3-SP2 candidate semantics before independent review exists."""

    failures: list[dict[str, Any]] = []
    question = original_question.strip()
    request_visual_preference = _normalized_visual_preference(
        visual_preference or plan.get("visual_preference")
    )
    raw_requirements = plan.get("requirement_coverage_map", [])
    if not isinstance(raw_requirements, list):
        failures.append({"code": "requirement_coverage_map_not_list"})
        raw_requirements = []
    requirements = [
        requirement
        for requirement in raw_requirements
        if isinstance(requirement, Mapping)
    ]
    raw_angles = plan.get("angles", [])
    if not isinstance(raw_angles, list):
        failures.append({"code": "candidate_angles_not_list"})
        raw_angles = []
    angles = [
        angle for angle in raw_angles if isinstance(angle, Mapping)
    ]
    for index, angle in enumerate(raw_angles, start=1):
        if not isinstance(angle, Mapping):
            failures.append(
                {
                    "code": "semantic_angle_not_object",
                    "angle_index": index,
                }
            )
    raw_tasks = plan.get("bounded_tasks", [])
    if not isinstance(raw_tasks, list):
        failures.append({"code": "candidate_bounded_tasks_not_list"})
        raw_tasks = []
    tasks = [
        task for task in raw_tasks if isinstance(task, Mapping)
    ]
    for index, task in enumerate(raw_tasks, start=1):
        if not isinstance(task, Mapping):
            failures.append(
                {
                    "code": "bounded_task_not_object",
                    "task_index": index,
                }
            )
    scope = str(plan.get("question_scope") or "")
    candidate_question_class = _candidate_validation_question_class(
        plan=plan,
        angles=angles,
        requirements=requirements,
    )
    expected_needs = _candidate_validation_expected_needs(plan=plan, angles=angles)
    effective_broad_question = _effective_broad_question(
        question_class=candidate_question_class,
        expected_needs=expected_needs,
        declared_broad=scope == "broad",
    )
    if effective_broad_question and not (5 <= len(angles) <= 8):
        failures.append(
            {
                "code": "broad_question_angle_count_out_of_range",
                "declared_question_scope": scope,
                "question_class": candidate_question_class,
                "angle_count": len(angles),
                "expected_evidence_needs": expected_needs,
            }
        )
    if effective_broad_question and not (20 <= len(tasks) <= 40):
        failures.append(
            {
                "code": "broad_question_task_count_out_of_range",
                "declared_question_scope": scope,
                "question_class": candidate_question_class,
                "task_count": len(tasks),
                "expected_evidence_needs": expected_needs,
            }
        )
    if not effective_broad_question and scope == "narrow" and tasks and not (6 <= len(tasks) <= 12):
        failures.append({"code": "narrow_task_count_out_of_range"})
    angle_ids = {str(angle.get("angle_id") or "") for angle in angles}
    task_ids = {str(task.get("task_id") or "") for task in tasks}
    executable_text = _candidate_executable_text(angles=angles, tasks=tasks)
    subject_tokens = _core_question_tokens(question)
    if subject_tokens and not _candidate_text_covers_subject(executable_text, subject_tokens):
        failures.append({"code": "subject_requirement_drift"})
    placeholder_labels = _candidate_placeholder_jurisdiction_labels(
        angles=angles,
        tasks=tasks,
        constraints=plan.get("constraints") if isinstance(plan.get("constraints"), list) else [],
    )
    if placeholder_labels:
        constraints_for_placeholder = (
            plan.get("constraints") if isinstance(plan.get("constraints"), list) else []
        )
        unbound_placeholder_labels = _candidate_unbound_placeholder_jurisdiction_labels(
            plan=plan,
            placeholder_labels=placeholder_labels,
        )
        if unbound_placeholder_labels:
            failures.append(
                {
                    "code": "unbound_jurisdiction_placeholders",
                    "placeholder_labels": unbound_placeholder_labels[:10],
                    "placeholder_label_count": len(unbound_placeholder_labels),
                    "requires_concrete_binding": True,
                    "message": (
                        "Placeholder jurisdiction labels must be bound to concrete "
                        "named jurisdictions in placeholder_binding metadata."
                    ),
                }
            )
        if unbound_placeholder_labels and not _candidate_has_placeholder_selection_workflow(
            angles=angles,
            tasks=tasks,
            constraints=constraints_for_placeholder,
        ):
            failures.append(
                {
                    "code": "missing_placeholder_selection_workflow",
                    "placeholder_labels": unbound_placeholder_labels[:10],
                    "message": (
                        "Placeholder jurisdiction labels require a selection, matching, "
                        "or binding workflow before evidence collection."
                    ),
                }
            )
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
        if (
            requirement.get("non_negotiable") is True
            and _candidate_requirement_is_req003_comparison(requirement)
        ):
            if not _candidate_has_comparison_deliverable_task(
                tasks=tasks,
                requirement=requirement,
            ):
                failures.append(
                    {
                        "code": "REQ_003_COMPARISON_DELIVERABLE_INCOMPLETE",
                        "requirement_id": requirement.get("requirement_id"),
                        "message": (
                            "Canonical req_003 comparison/output-shape requirements "
                            "need a bounded side-by-side comparison deliverable with "
                            "match/partial/mismatch/unverifiable, evidence, caveat, "
                            "and remediation fields."
                        ),
                    }
                )
            if (
                _candidate_requirement_needs_prioritized_remediation(requirement)
                and not _candidate_has_prioritized_remediation_task(tasks=tasks)
            ):
                failures.append(
                    {
                        "code": "REQ_003_PRIORITIZED_REMEDIATION_MISSING",
                        "requirement_id": requirement.get("requirement_id"),
                    }
                )
        if (
            requirement.get("non_negotiable") is True
            and _candidate_requirement_needs_structured_artifact_assessment(requirement)
            and not _candidate_has_structured_artifact_assessment_task(tasks=tasks)
        ):
            failures.append(
                {
                    "code": "STRUCTURED_ARTIFACT_ROUTE_INCOMPLETE",
                    "requirement_id": requirement.get("requirement_id"),
                    "message": (
                        "Structured model/artifact requirements need at least one "
                        "text/document structured-artifact assessment task; visual "
                        "inspection alone is not enough."
                    ),
                }
            )
    tasks_per_angle = Counter(str(task.get("angle_id") or "") for task in tasks)
    if effective_broad_question:
        for angle_id in angle_ids:
            if tasks_per_angle[angle_id] < 2:
                failures.append(
                    {
                        "code": "broad_angle_has_too_few_tasks",
                        "angle_id": angle_id,
                    }
                )
    required_angle_fields = (
        "angle_id",
        "title",
        "research_question",
        "why_this_angle_matters",
        "included_scope",
        "excluded_scope",
        "route",
        "evidence_need",
        "expected_source_types",
        "expected_visual_targets",
        "expected_artifacts",
        "search_queries",
        "success_criteria",
        "report_section",
        "risk_or_contradiction_checks",
    )
    required_angle_list_fields = (
        "included_scope",
        "excluded_scope",
        "expected_source_types",
        "expected_visual_targets",
        "expected_artifacts",
        "search_queries",
        "success_criteria",
        "risk_or_contradiction_checks",
    )
    for angle in angles:
        angle_id = angle.get("angle_id")
        for field_name in required_angle_fields:
            if field_name not in angle:
                failures.append(
                    {
                        "code": "semantic_angle_missing_field",
                        "angle_id": angle_id,
                        "field": field_name,
                    }
                )
        for field_name in required_angle_list_fields:
            if field_name in angle and not isinstance(angle.get(field_name), list):
                failures.append(
                    {
                        "code": "semantic_angle_invalid_field_type",
                        "angle_id": angle_id,
                        "field": field_name,
                        "expected_type": "list",
                    }
                )
    failures.extend(
        _candidate_semantic_angle_release_depth_failures(
            question=question,
            angles=angles,
        )
    )
    failures.extend(
        _candidate_semantic_angle_release_duplicate_failures(
            question=question,
            angles=angles,
        )
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
    required_task_list_fields = (
        "expected_source_types",
        "expected_visual_targets",
        "expected_artifacts",
        "success_criteria",
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
        for field_name in required_task_list_fields:
            if field_name in task and not isinstance(task.get(field_name), list):
                failures.append(
                    {
                        "code": "bounded_task_invalid_field_type",
                        "task_id": task.get("task_id"),
                        "field": field_name,
                        "expected_type": "list",
                    }
                )
        if "source_policy" in task and not isinstance(task.get("source_policy"), Mapping):
            failures.append(
                {
                    "code": "bounded_task_invalid_field_type",
                    "task_id": task.get("task_id"),
                    "field": "source_policy",
                    "expected_type": "object",
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
        route = str(task.get("route") or "text_only")
        visual_targets = _string_list(task.get("expected_visual_targets"))
        task_expected_evidence = set(_string_list(task.get("expected_evidence")))
        if (
            route == "text_only"
            and task_expected_evidence & VISUAL_EXPECTED_EVIDENCE
        ):
            failures.append(
                {
                    "code": "text_only_task_visual_expected_evidence",
                    "task_id": task.get("task_id"),
                    "expected_evidence": sorted(
                        task_expected_evidence & VISUAL_EXPECTED_EVIDENCE
                    ),
                }
            )
        if route != "text_only" or visual_targets:
            try:
                max_images = int(task.get("max_images") or 0)
            except (TypeError, ValueError):
                max_images = 0
            if max_images <= 0:
                failures.append(
                    {
                        "code": "visual_expected_evidence_without_image_budget",
                        "task_id": task.get("task_id"),
                    }
                )
    failures.extend(_candidate_task_cap_feasibility_failures(tasks))
    failures.extend(
        _candidate_source_cap_constraint_failures(
            plan=plan,
            constraints=plan.get("constraints"),
            tasks=tasks,
        )
    )
    if request_visual_preference == "text_only":
        text_only_violations: list[dict[str, Any]] = []
        for angle in angles:
            angle_id = angle.get("angle_id")
            route = str(angle.get("route") or "text_only")
            if route in {"visual_required", "visual_optional"}:
                text_only_violations.append(
                    {
                        "record_type": "angle",
                        "angle_id": angle_id,
                        "field": "route",
                        "value": route,
                    }
                )
            visual_targets = _string_list(angle.get("expected_visual_targets"))
            if visual_targets:
                text_only_violations.append(
                    {
                        "record_type": "angle",
                        "angle_id": angle_id,
                        "field": "expected_visual_targets",
                        "value_count": len(visual_targets),
                    }
                )
        for task in tasks:
            task_id = task.get("task_id")
            route = str(task.get("route") or "text_only")
            if route in {"visual_required", "visual_optional"}:
                text_only_violations.append(
                    {
                        "record_type": "task",
                        "task_id": task_id,
                        "angle_id": task.get("angle_id"),
                        "field": "route",
                        "value": route,
                    }
                )
            visual_targets = _string_list(task.get("expected_visual_targets"))
            if visual_targets:
                text_only_violations.append(
                    {
                        "record_type": "task",
                        "task_id": task_id,
                        "angle_id": task.get("angle_id"),
                        "field": "expected_visual_targets",
                        "value_count": len(visual_targets),
                    }
                )
            try:
                max_images = int(task.get("max_images") or 0)
            except (TypeError, ValueError):
                max_images = 0
            if max_images > 0:
                text_only_violations.append(
                    {
                        "record_type": "task",
                        "task_id": task_id,
                        "angle_id": task.get("angle_id"),
                        "field": "max_images",
                        "value": max_images,
                    }
                )
        for angle in angles:
            _append_text_only_visual_work_violations(
                text_only_violations,
                record_type="angle",
                record=angle,
                fields=TEXT_ONLY_ANGLE_VISUAL_WORK_FIELDS,
            )
        for task in tasks:
            _append_text_only_visual_work_violations(
                text_only_violations,
                record_type="task",
                record=task,
                fields=TEXT_ONLY_TASK_VISUAL_WORK_FIELDS,
            )
        if text_only_violations:
            failures.append(
                {
                    "code": "text_only_visual_preference_violation",
                    "visual_preference": request_visual_preference,
                    "message": (
                        "visual_preference=text_only forbids visual routes, visual "
                        "targets, positive image budgets, visual evidence needs, "
                        "and explicit visual work in executable fields."
                    ),
                    "violations": text_only_violations[:20],
                    "violation_count": len(text_only_violations),
                }
            )
    visual_optional_support_profile: dict[str, Any] = {}
    if _visual_optional_without_primary_visual_evidence(
        question=question,
        visual_preference=request_visual_preference,
        provided_images=provided_images,
    ):
        visual_optional_support_profile = _candidate_visual_support_profile(
            angles=angles,
            tasks=tasks,
        )
        if (
            visual_optional_support_profile["visual_required_angle_count"]
            or visual_optional_support_profile["visual_required_task_count"]
        ):
            failures.append(
                {
                    "code": "MODALITY_OPTIONALITY_REVERSED",
                    "visual_preference": request_visual_preference,
                    "message": (
                        "visual_optional without a supplied image or explicit visual "
                        "evidence request must use bounded visual_optional support, "
                        "not visual_required routes."
                    ),
                    "profile": dict(visual_optional_support_profile),
                }
            )
        if (
            visual_optional_support_profile["visual_angle_count"]
            > visual_optional_support_profile["max_visual_angles"]
            or visual_optional_support_profile["visual_task_count"]
            > visual_optional_support_profile["max_visual_tasks"]
            or (
                visual_optional_support_profile["text_task_count"] > 0
                and visual_optional_support_profile["visual_task_count"]
                >= visual_optional_support_profile["text_task_count"]
            )
        ):
            failures.append(
                {
                    "code": "visual_optional_visual_work_dominates_primary_evidence",
                    "visual_preference": request_visual_preference,
                    "message": (
                        "visual_optional support is too large for a prompt whose "
                        "primary evidence is text/document/table/structured artifact."
                    ),
                    "profile": dict(visual_optional_support_profile),
                }
            )

    visual_required = request_visual_preference in {
        "visual_required",
        "visual_optional",
    } or (
        request_visual_preference != "text_only"
        and (
            any(
                str(requirement.get("requirement_type") or "") == "visual_modality"
                for requirement in requirements
            )
            or question_mentions_visual_evidence(question)
        )
    )
    if visual_required:
        visual_angles = [
            angle for angle in angles if str(angle.get("route") or "") != "text_only"
        ]
        visual_tasks = [
            task for task in tasks if str(task.get("route") or "") != "text_only"
        ]
        visual_evidence_needs = {
            str(angle.get("evidence_need") or "")
            for angle in visual_angles
        }
        visual_evidence_hits = _visual_expected_evidence_hits(
            visual_angles,
            visual_tasks,
        )
        if not visual_angles or not visual_tasks:
            failures.append({"code": "visual_requirement_missing_visual_route"})
            if not visual_angles and not visual_tasks:
                failures.append(
                    {
                        "code": "visual_question_all_text_only",
                        "message": (
                            "Visual-required or visual-optional prompts must not produce "
                            "only text-only angles and bounded tasks."
                        ),
                    }
                )
        if not any(_string_list(task.get("expected_visual_targets")) for task in visual_tasks):
            failures.append({"code": "visual_requirement_missing_targets"})
        if request_visual_preference == "visual_optional":
            if int(visual_evidence_hits.get("visual_example") or 0) < 1:
                failures.append({"code": "visual_example_expected_evidence_missing"})
            observation_hits = int(
                visual_evidence_hits.get("visual_observation") or 0
            ) + int(visual_evidence_hits.get("vlm_analysis") or 0)
            if observation_hits < 1:
                failures.append({"code": "visual_observation_expected_evidence_missing"})
        else:
            if "visual_example" not in visual_evidence_needs:
                failures.append({"code": "visual_example_expected_evidence_missing"})
            if not (visual_evidence_needs & {"visual_observation", "vlm_analysis"}):
                failures.append({"code": "visual_observation_expected_evidence_missing"})
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
        "declared_question_scope": scope,
        "effective_broad_question": effective_broad_question,
        "question_class": candidate_question_class,
        "expected_evidence_needs": expected_needs,
        "angle_count": len(angles),
        "task_count": len(tasks),
        "visual_optional_support_profile": visual_optional_support_profile,
        "failure_count": len(failures),
        "failures": failures,
        "ok": not failures,
    }


def _candidate_semantic_angle_release_depth_failures(
    *,
    question: str,
    angles: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    original_tokens = _semantic_release_meaningful_tokens(question)
    if angles and not original_tokens:
        return [
            {
                "code": "semantic_angle_release_depth_failed",
                "angle_id": None,
                "angle_index": None,
                "reasons": ["original_question_missing_meaningful_tokens"],
                "minimum_meaningful_overlap": SEMANTIC_RELEASE_MIN_ANGLE_OVERLAP_TOKENS,
            }
        ]

    failures: list[dict[str, Any]] = []
    for index, angle in enumerate(angles, start=1):
        angle_id = str(angle.get("angle_id") or f"angle_{index:03d}")
        title = str(angle.get("title") or "").strip()
        research_question = str(angle.get("research_question") or "").strip()
        reasons: list[str] = []
        missing_string_fields = [
            field_name
            for field_name in (
                "angle_id",
                "title",
                "research_question",
                "evidence_need",
                "report_section",
            )
            if not _semantic_release_non_empty_string(angle.get(field_name))
        ]
        missing_list_fields = [
            field_name
            for field_name in ("expected_artifacts", "success_criteria")
            if not _semantic_release_non_empty_string_list(angle.get(field_name))
        ]
        if missing_string_fields:
            reasons.append("required_release_string_field_missing")
        if missing_list_fields:
            reasons.append("required_release_list_field_missing")
        if title and _semantic_release_generic_or_original_text(title, question):
            reasons.append("title_generic_or_original")
        if research_question and _semantic_release_generic_or_original_text(
            research_question,
            question,
        ):
            reasons.append("research_question_generic_or_original")

        combined_text = f"{title} {research_question}".strip()
        if combined_text and _semantic_release_placeholder_text(combined_text):
            reasons.append("placeholder_angle_text")

        all_tokens = _semantic_release_token_list(combined_text)
        meaningful_tokens = _semantic_release_meaningful_token_list(combined_text)
        unique_meaningful_tokens = set(meaningful_tokens)
        if len(unique_meaningful_tokens) < SEMANTIC_RELEASE_MIN_ANGLE_UNIQUE_TOKENS:
            reasons.append("too_few_unique_meaningful_tokens")
        if all_tokens and len(meaningful_tokens) * 2 < len(all_tokens):
            reasons.append("too_much_generic_or_shallow_text")
        overlap_tokens = unique_meaningful_tokens & original_tokens
        if len(overlap_tokens) < SEMANTIC_RELEASE_MIN_ANGLE_OVERLAP_TOKENS:
            reasons.append("meaningful_overlap_too_low")

        if reasons:
            failures.append(
                {
                    "code": "semantic_angle_release_depth_failed",
                    "angle_id": angle_id,
                    "angle_index": index,
                    "message": (
                        "Candidate semantic angle title+research_question is too "
                        "shallow for release semantic angle validation."
                    ),
                    "reasons": list(dict.fromkeys(reasons)),
                    "missing_string_fields": missing_string_fields,
                    "missing_list_fields": missing_list_fields,
                    "meaningful_overlap_count": len(overlap_tokens),
                    "minimum_meaningful_overlap": (
                        SEMANTIC_RELEASE_MIN_ANGLE_OVERLAP_TOKENS
                    ),
                    "overlap_tokens": sorted(overlap_tokens),
                    "unique_meaningful_token_count": len(unique_meaningful_tokens),
                    "minimum_unique_meaningful_tokens": (
                        SEMANTIC_RELEASE_MIN_ANGLE_UNIQUE_TOKENS
                    ),
                    "title": _preview_text(title),
                    "research_question": _preview_text(research_question),
                }
            )
    return failures


def _candidate_semantic_angle_release_duplicate_failures(
    *,
    question: str,
    angles: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    original_tokens = _semantic_release_meaningful_tokens(question)
    angle_token_records = [
        {
            "angle_id": str(angle.get("angle_id") or f"angle_{index:03d}"),
            "angle_index": index,
            "tokens": _semantic_release_angle_content_tokens(angle),
        }
        for index, angle in enumerate(angles, start=1)
    ]
    duplicate_pairs: list[dict[str, Any]] = []
    for left_index, left_record in enumerate(angle_token_records):
        for right_record in angle_token_records[left_index + 1:]:
            similarity = _semantic_release_angle_token_similarity(
                left_record["tokens"],
                right_record["tokens"],
            )
            shared_prompt_anchor_tokens = (
                left_record["tokens"] & right_record["tokens"] & original_tokens
            )
            left_distinguishing_tokens = (
                left_record["tokens"] - shared_prompt_anchor_tokens
            )
            right_distinguishing_tokens = (
                right_record["tokens"] - shared_prompt_anchor_tokens
            )
            distinguishing_similarity = _semantic_release_angle_token_similarity(
                left_distinguishing_tokens,
                right_distinguishing_tokens,
            )
            distinguishing_delta_tokens = (
                left_distinguishing_tokens ^ right_distinguishing_tokens
            )
            substantive_distinguishing_tokens = (
                _semantic_release_substantive_distinguishing_tokens(
                    distinguishing_delta_tokens
                )
            )
            exact_duplicate = (
                tuple(sorted(left_record["tokens"]))
                == tuple(sorted(right_record["tokens"]))
            )
            distinguishing_near_duplicate = (
                distinguishing_similarity["jaccard"]
                >= SEMANTIC_ANGLE_NEAR_DUPLICATE_THRESHOLD
                or distinguishing_similarity["containment"]
                >= SEMANTIC_ANGLE_NEAR_DUPLICATE_THRESHOLD
            )
            near_duplicate = (
                (
                    similarity["jaccard"] >= SEMANTIC_ANGLE_NEAR_DUPLICATE_THRESHOLD
                    or similarity["containment"]
                    >= SEMANTIC_ANGLE_NEAR_DUPLICATE_THRESHOLD
                )
                and distinguishing_near_duplicate
            )
            contained_suffix_duplicate = (
                _semantic_release_contained_prompt_anchor_suffix_duplicate(
                    similarity=similarity,
                    distinguishing_delta_tokens=distinguishing_delta_tokens,
                    substantive_distinguishing_tokens=(
                        substantive_distinguishing_tokens
                    ),
                )
            )
            if (
                not exact_duplicate
                and not near_duplicate
                and not contained_suffix_duplicate
            ):
                continue
            reason = "near_duplicate_token_overlap"
            if contained_suffix_duplicate:
                reason = "contained_prompt_anchor_suffix_duplicate"
            if exact_duplicate:
                reason = "exact_token_signature"
            duplicate_pairs.append(
                {
                    "left_angle_id": left_record["angle_id"],
                    "left_angle_index": left_record["angle_index"],
                    "right_angle_id": right_record["angle_id"],
                    "right_angle_index": right_record["angle_index"],
                    "reason": reason,
                    "jaccard": similarity["jaccard"],
                    "containment": similarity["containment"],
                    "distinguishing_jaccard": distinguishing_similarity["jaccard"],
                    "distinguishing_containment": distinguishing_similarity[
                        "containment"
                    ],
                    "threshold": SEMANTIC_ANGLE_NEAR_DUPLICATE_THRESHOLD,
                    "shared_tokens": sorted(
                        left_record["tokens"] & right_record["tokens"]
                    )[:20],
                    "shared_prompt_anchor_tokens": sorted(
                        shared_prompt_anchor_tokens
                    )[:20],
                    "shared_distinguishing_tokens": sorted(
                        left_distinguishing_tokens & right_distinguishing_tokens
                    )[:20],
                    "distinguishing_delta_tokens": sorted(
                        distinguishing_delta_tokens
                    )[:20],
                    "substantive_distinguishing_tokens": sorted(
                        substantive_distinguishing_tokens
                    )[:20],
                    "non_substantive_distinguishing_tokens": sorted(
                        distinguishing_delta_tokens
                        - substantive_distinguishing_tokens
                    )[:20],
                    "left_unique_meaningful_token_count": len(left_record["tokens"]),
                    "right_unique_meaningful_token_count": len(
                        right_record["tokens"]
                    ),
                }
            )
    if not duplicate_pairs:
        return []
    return [
        {
            "code": "semantic_angle_release_duplicate_failed",
            "message": (
                "Candidate semantic angle title+research_question values contain "
                "duplicate or near-duplicate release content."
            ),
            "duplicate_pair_count": len(duplicate_pairs),
            "duplicate_pairs": duplicate_pairs[:10],
            "colliding_angle_ids": sorted(
                {
                    str(pair["left_angle_id"])
                    for pair in duplicate_pairs
                }
                | {
                    str(pair["right_angle_id"])
                    for pair in duplicate_pairs
                }
            ),
            "colliding_angle_indexes": sorted(
                {
                    int(pair["left_angle_index"])
                    for pair in duplicate_pairs
                }
                | {
                    int(pair["right_angle_index"])
                    for pair in duplicate_pairs
                }
            ),
            "threshold": SEMANTIC_ANGLE_NEAR_DUPLICATE_THRESHOLD,
        }
    ]


def _semantic_release_angle_content_tokens(angle: Mapping[str, Any]) -> set[str]:
    return set(
        _semantic_release_meaningful_token_list(
            f"{angle.get('title') or ''} {angle.get('research_question') or ''}"
        )
    )


def _semantic_release_angle_token_similarity(
    left: set[str],
    right: set[str],
) -> dict[str, float]:
    intersection = len(left & right)
    union = len(left | right)
    smaller = min(len(left), len(right))
    if not union:
        return {"jaccard": 1.0, "containment": 1.0}
    return {
        "jaccard": intersection / union,
        "containment": intersection / smaller if smaller else 0.0,
    }


def _semantic_release_contained_prompt_anchor_suffix_duplicate(
    *,
    similarity: Mapping[str, float],
    distinguishing_delta_tokens: set[str],
    substantive_distinguishing_tokens: set[str],
) -> bool:
    if similarity["containment"] < SEMANTIC_ANGLE_NEAR_DUPLICATE_THRESHOLD:
        return False
    return not substantive_distinguishing_tokens


def _semantic_release_substantive_distinguishing_tokens(
    tokens: set[str],
) -> set[str]:
    return {
        token
        for token in tokens
        if token not in SEMANTIC_RELEASE_GENERIC_TOKENS
        and token not in SEMANTIC_RELEASE_NON_SUBSTANTIVE_SUFFIX_TOKENS
        and not token.isdigit()
    }


def _semantic_release_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _semantic_release_non_empty_string_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_semantic_release_non_empty_string(item) for item in value)
    )


def _semantic_release_generic_or_original_text(
    text: str,
    original_question: str,
) -> bool:
    normalized = _semantic_release_normalized_text(text)
    if normalized in SEMANTIC_RELEASE_GENERIC_ANGLE_TEXTS:
        return True
    return bool(original_question) and normalized == _semantic_release_normalized_text(
        original_question
    )


def _semantic_release_placeholder_text(text: str) -> bool:
    normalized = _semantic_release_normalized_text(text)
    return any(
        re.search(pattern, normalized)
        for pattern in SEMANTIC_RELEASE_GENERIC_PLACEHOLDER_PATTERNS
    )


def _semantic_release_token_list(text: str) -> list[str]:
    tokens: list[str] = []
    for token in re.findall(r"[A-Za-z0-9\uac00-\ud7a3]+", text.lower()):
        if _semantic_release_token_has_hangul(token):
            normalized = _normalize_korean_semantic_token(token)
            if len(normalized) >= 2:
                tokens.append(normalized)
        elif len(token) > 2:
            tokens.append(token)
    return tokens


def _semantic_release_token_has_hangul(token: str) -> bool:
    return any("\uac00" <= char <= "\ud7a3" for char in token)


def _normalize_korean_semantic_token(token: str) -> str:
    for suffix in KOREAN_SEMANTIC_PARTICLE_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            return token[: -len(suffix)]
    return token


def _semantic_release_meaningful_token_list(text: str) -> list[str]:
    return [
        token
        for token in _semantic_release_token_list(text)
        if token not in SEMANTIC_RELEASE_GENERIC_TOKENS
    ]


def _semantic_release_meaningful_tokens(text: str) -> set[str]:
    return set(_semantic_release_meaningful_token_list(text))


def _semantic_release_normalized_text(text: str) -> str:
    return " ".join(re.findall(r"[A-Za-z0-9\uac00-\ud7a3]+", text.lower()))


def _candidate_validation_question_class(
    *,
    plan: Mapping[str, Any],
    angles: Sequence[Mapping[str, Any]],
    requirements: Sequence[Mapping[str, Any]],
) -> str:
    candidate_class = str(plan.get("question_class") or "")
    if candidate_class in {
        _CLASS_GENERAL,
        _CLASS_TECHNICAL,
        _CLASS_PRODUCT,
        _CLASS_VISUAL,
        _CLASS_POLICY,
        _CLASS_IMPLEMENTATION,
    }:
        return candidate_class
    if any(str(angle.get("route") or "text_only") != "text_only" for angle in angles):
        return _CLASS_VISUAL
    if any(
        str(requirement.get("requirement_type") or "")
        in {"source_quality", "safety_risk"}
        for requirement in requirements
    ):
        return _CLASS_POLICY
    return _CLASS_GENERAL


def _candidate_validation_expected_needs(
    *,
    plan: Mapping[str, Any],
    angles: Sequence[Mapping[str, Any]],
) -> list[str]:
    expected_needs = _string_list(plan.get("expected_evidence_needs"))
    if expected_needs:
        return expected_needs
    return _ordered_unique(
        str(angle.get("evidence_need") or "")
        for angle in angles
        if str(angle.get("evidence_need") or "").strip()
    )


def _effective_broad_question(
    *,
    question_class: str,
    expected_needs: Sequence[str],
    declared_broad: bool,
) -> bool:
    if declared_broad:
        return True
    return question_class != _CLASS_GENERAL and len(set(expected_needs)) >= 4


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
    normalized_visual_preference = _normalized_visual_preference(visual_preference)
    request_budget_cap = dict(budget_cap)
    planner_instructions = [
        "Decompose the user's raw question into prompt-specific research angles.",
        "Preserve every explicit requirement and justified inferred constraint.",
        "Create bounded executable research tasks with source, visual, and done-condition fields.",
        "Materialize budget_cap.max_results as a search result cap in plan constraints and bounded_tasks.max_results.",
        "Every angle title plus research_question must be prompt-specific and share at least 2 meaningful non-generic domain/source/comparison tokens with the original question.",
        "Do not use hidden template classes, fixed domain menus, canned task lists, or copied angle titles.",
    ]
    if normalized_visual_preference == "text_only":
        request_budget_cap["max_images"] = 0
        planner_instructions.append(_text_only_visual_contract_instruction())
    elif normalized_visual_preference == "visual_optional":
        planner_instructions.append(
            _visual_optional_contract_instruction(
                has_provided_images=bool(provided_images),
                prompt_mentions_visual=question_mentions_visual_evidence(question),
            )
        )
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
        "visual_preference": normalized_visual_preference,
        "budget_cap": request_budget_cap,
        "provided_sources": [dict(item) for item in provided_sources],
        "provided_images": [dict(item) for item in provided_images],
        "planner_instructions": planner_instructions,
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
            "bounded_tasks[].max_results": "integer from budget_cap.max_results",
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
    max_results = max(1, int(budget_cap.get("max_results") or source_cap))
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
                    "max_results": max_results,
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


PLACEHOLDER_JURISDICTION_PATTERN = re.compile(
    r"\b(?:municipality|municipalities|jurisdiction|jurisdictions|city|cities|county|counties|region|regions|province|provinces|locality|localities|district|districts)\s*(?:#|no\.?|number)?\s*\d+\b",
    re.IGNORECASE,
)


def _candidate_placeholder_jurisdiction_labels(
    *,
    angles: Sequence[Mapping[str, Any]],
    tasks: Sequence[Mapping[str, Any]],
    constraints: Sequence[Any],
) -> list[str]:
    text = _candidate_placeholder_jurisdiction_text(
        angles=angles,
        tasks=tasks,
        constraints=constraints,
    )
    labels = [
        " ".join(match.group(0).lower().split())
        for match in PLACEHOLDER_JURISDICTION_PATTERN.finditer(text)
    ]
    return _ordered_unique(labels)


def _candidate_has_placeholder_selection_workflow(
    *,
    angles: Sequence[Mapping[str, Any]],
    tasks: Sequence[Mapping[str, Any]],
    constraints: Sequence[Any],
) -> bool:
    text = _normalize_text(
        _candidate_placeholder_jurisdiction_text(
            angles=angles,
            tasks=tasks,
            constraints=constraints,
        )
    )
    if not text:
        return False
    selection_phrase = (
        r"(?:select|selection|selected|choose|chosen|shortlist|sampling|sample|"
        r"bind|binding|map|mapped|mapping|match|matching|identify|identified|"
        r"name|named)"
    )
    jurisdiction_phrase = (
        r"(?:placeholder|municipality|municipalities|jurisdiction|jurisdictions|"
        r"city|cities|county|counties|region|regions|locality|localities|"
        r"district|districts)"
    )
    proximity_patterns = (
        rf"\b{selection_phrase}\b(?:\s+\w+){{0,12}}\s+\b{jurisdiction_phrase}\b",
        rf"\b{jurisdiction_phrase}\b(?:\s+\w+){{0,12}}\s+\b{selection_phrase}\b",
        r"(?:선정|선택|매핑|대응)(?:\s+\w+){0,12}\s+(?:지자체|관할|지역|도시)",
        r"(?:지자체|관할|지역|도시)(?:\s+\w+){0,12}\s+(?:선정|선택|매핑|대응)",
    )
    return any(re.search(pattern, text) for pattern in proximity_patterns)


def _candidate_record_mentions_placeholder_jurisdiction(record: Mapping[str, Any]) -> bool:
    text = json.dumps(record, ensure_ascii=False, sort_keys=True)
    return bool(PLACEHOLDER_JURISDICTION_PATTERN.search(text))


def _candidate_placeholder_jurisdiction_text(
    *,
    angles: Sequence[Mapping[str, Any]],
    tasks: Sequence[Mapping[str, Any]],
    constraints: Sequence[Any],
) -> str:
    values: list[Any] = [constraints]
    for angle in angles:
        values.extend(
            [
                angle.get("title"),
                angle.get("research_question"),
                angle.get("why_this_angle_matters"),
                angle.get("included_scope"),
                angle.get("excluded_scope"),
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
                task.get("expected_artifacts"),
                task.get("success_criteria"),
                task.get("done_condition"),
            ]
        )
    return json.dumps(values, ensure_ascii=False, sort_keys=True)


def _candidate_placeholder_binding_map(plan: Mapping[str, Any]) -> dict[str, Any]:
    for field_name in (
        "placeholder_binding",
        "placeholder_bindings",
        "placeholder_jurisdiction_binding",
        "placeholder_jurisdiction_bindings",
    ):
        value = plan.get(field_name)
        if isinstance(value, Mapping):
            return {str(key).lower(): copy.deepcopy(binding) for key, binding in value.items()}
    return {}


def _candidate_unbound_placeholder_jurisdiction_labels(
    *,
    plan: Mapping[str, Any],
    placeholder_labels: Sequence[str],
) -> list[str]:
    binding_map = _candidate_placeholder_binding_map(plan)
    unbound: list[str] = []
    for label in placeholder_labels:
        normalized_label = str(label).lower()
        binding = binding_map.get(normalized_label)
        if not _candidate_placeholder_binding_is_concrete(binding):
            unbound.append(str(label))
    return _ordered_unique(unbound)


def _candidate_placeholder_binding_is_concrete(binding: Any) -> bool:
    if isinstance(binding, Mapping):
        for field_name in (
            "jurisdiction_name",
            "name",
            "bound_to",
            "selected_jurisdiction",
            "municipality_name",
        ):
            value = binding.get(field_name)
            if _candidate_placeholder_binding_name_is_concrete(value):
                return True
        return False
    return _candidate_placeholder_binding_name_is_concrete(binding)


def _candidate_placeholder_binding_name_is_concrete(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    name = " ".join(value.split())
    if not name:
        return False
    lowered = name.lower()
    if PLACEHOLDER_JURISDICTION_PATTERN.search(name):
        return False
    generic_phrases = (
        "named jurisdiction",
        "selected jurisdiction",
        "selected municipality",
        "chosen jurisdiction",
        "chosen municipality",
        "placeholder",
        "to be selected",
        "tbd",
        "n/a",
    )
    if any(phrase in lowered for phrase in generic_phrases):
        return False
    return bool(re.search(r"[A-Za-z\uac00-\ud7a3]", name))


def _candidate_materialized_placeholder_bindings(
    candidate: Mapping[str, Any],
    *,
    placeholder_labels: Sequence[str],
) -> dict[str, dict[str, Any]]:
    jurisdiction_names = _candidate_context_jurisdiction_names(
        candidate,
        needed=len(placeholder_labels),
    )
    if len(jurisdiction_names) < len(placeholder_labels):
        return {}
    bindings: dict[str, dict[str, Any]] = {}
    for label, jurisdiction_name in zip(placeholder_labels, jurisdiction_names):
        bindings[str(label)] = {
            "jurisdiction_name": jurisdiction_name,
            "binding_source": "deterministic_question_context",
            "selection_basis": (
                "Derived from the geographic context in the original question and "
                "ordered deterministically for reproducible release validation."
            ),
        }
    return bindings


def _candidate_context_jurisdiction_names(
    candidate: Mapping[str, Any],
    *,
    needed: int,
) -> list[str]:
    values = [
        candidate.get("original_question"),
        candidate.get("intent_summary"),
        candidate.get("domain_entities"),
        candidate.get("constraints"),
        candidate.get("requirement_coverage_map"),
    ]
    text = json.dumps(values, ensure_ascii=False, sort_keys=True)
    lowered = text.lower()
    explicit_names = _candidate_explicit_jurisdiction_names(candidate)
    if len(explicit_names) >= needed:
        return explicit_names[:needed]
    if (
        "south korea" in lowered
        or "korea" in lowered
        or "대한민국" in text
        or "한국" in text
    ):
        defaults = [
            "Seoul, South Korea",
            "Busan, South Korea",
            "Incheon, South Korea",
            "Daegu, South Korea",
            "Daejeon, South Korea",
            "Gwangju, South Korea",
            "Ulsan, South Korea",
            "Sejong, South Korea",
        ]
        return _ordered_unique([*explicit_names, *defaults])[:needed]
    if "united states" in lowered or "u.s." in lowered or " usa" in f" {lowered}":
        defaults = [
            "New York City, United States",
            "Los Angeles, United States",
            "Chicago, United States",
            "Houston, United States",
            "Phoenix, United States",
            "Philadelphia, United States",
        ]
        return _ordered_unique([*explicit_names, *defaults])[:needed]
    return explicit_names[:needed]


def _candidate_explicit_jurisdiction_names(candidate: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for entity in _list(candidate.get("domain_entities")):
        if not isinstance(entity, Mapping):
            continue
        entity_type = str(entity.get("type") or "").lower()
        if not any(
            token in entity_type
            for token in ("jurisdiction", "municipality", "city", "county", "region")
        ):
            continue
        name = str(entity.get("name") or "").strip()
        if _candidate_placeholder_binding_name_is_concrete(name):
            names.append(name)
    return _ordered_unique(names)


def _candidate_record_placeholder_bindings(
    record: Mapping[str, Any],
    *,
    placeholder_binding: Mapping[str, Any],
) -> dict[str, Any]:
    if not placeholder_binding:
        return {}
    text = json.dumps(record, ensure_ascii=False, sort_keys=True).lower()
    bindings: dict[str, Any] = {}
    for label, binding in placeholder_binding.items():
        if str(label).lower() in text:
            bindings[str(label)] = copy.deepcopy(binding)
    return bindings


def _candidate_source_cap_constraint_failures(
    *,
    plan: Mapping[str, Any] | None = None,
    constraints: Any,
    tasks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    constraint_records = constraints if isinstance(constraints, list) else []
    caps = [
        cap
        for cap in (
            _semantic_task_source_cap_int(task.get("max_sources"))
            for task in tasks
            if isinstance(task, Mapping)
        )
        if cap is not None
    ]
    if not caps:
        return []
    failures: list[dict[str, Any]] = []
    executable_budgets: list[int] = []
    for index, constraint in enumerate(constraint_records, start=1):
        global_budget = _candidate_declared_global_source_budget(constraint)
        if (
            global_budget is not None
            and _candidate_constraint_preserves_executable_source_budget(
                constraint,
                declared_source_budget=global_budget,
            )
        ):
            executable_budgets.append(global_budget)
        conflict = _candidate_source_cap_constraint_conflict_record(
            constraint,
            normalized_caps=caps,
        )
        if not conflict:
            continue
        failures.append(
            {
                "code": "source_cap_constraint_conflicts_with_tasks",
                "constraint_index": index,
                "constraint": _preview_text(str(constraint), limit=240),
                **conflict,
            }
        )
    total_cap = sum(caps)
    runner_source_budget = (
        plan.get("runner_source_budget")
        if isinstance(plan, Mapping)
        else None
    )
    for declared_budget in sorted(set(executable_budgets)):
        if total_cap <= declared_budget:
            continue
        if not _candidate_runner_source_budget_preserves_cap(
            runner_source_budget,
            declared_source_budget=declared_budget,
            task_max_sources_sum=total_cap,
        ):
            failures.append(
                {
                    "code": "global_source_budget_missing_executable_runner_budget",
                    "declared_source_budget": declared_budget,
                    "task_max_sources_sum": total_cap,
                    "message": (
                        "Executable global source budgets must be preserved as "
                        "machine-readable runner_source_budget metadata."
                    ),
                }
            )
    runner_budget_cap = _candidate_runner_source_budget_cap(runner_source_budget)
    if (
        runner_budget_cap is not None
        and total_cap > runner_budget_cap
        and runner_budget_cap not in set(executable_budgets)
    ):
        failures.append(
            {
                "code": "global_source_budget_not_preserved_in_constraints",
                "declared_source_budget": runner_budget_cap,
                "task_max_sources_sum": total_cap,
                "message": (
                    "runner_source_budget metadata must also be visible in plan "
                    "constraints with the numeric global cap preserved."
                ),
            }
        )
    return failures


def _candidate_runner_source_budget_cap(runner_source_budget: Any) -> int | None:
    if not isinstance(runner_source_budget, Mapping):
        return None
    for field_name in ("max_unique_sources", "declared_source_budget", "max_sources"):
        value = _semantic_task_source_cap_int(runner_source_budget.get(field_name))
        if value is not None and value > 0:
            return value
    return None


def _candidate_runner_source_budget_preserves_cap(
    runner_source_budget: Any,
    *,
    declared_source_budget: int,
    task_max_sources_sum: int,
) -> bool:
    if not isinstance(runner_source_budget, Mapping):
        return False
    cap = _candidate_runner_source_budget_cap(runner_source_budget)
    if cap != declared_source_budget:
        return False
    budget_type = str(runner_source_budget.get("budget_type") or "").lower()
    allocation_strategy = str(runner_source_budget.get("allocation_strategy") or "").lower()
    enforcement_scope = str(runner_source_budget.get("enforcement_scope") or "").lower()
    if "unique" not in budget_type and "unique" not in allocation_strategy:
        return False
    if not (
        "reuse" in budget_type
        or "reuse" in allocation_strategy
        or "shared_source_pool" in allocation_strategy
        or "shared source pool" in allocation_strategy
    ):
        return False
    if enforcement_scope not in {"run", "runner", "runner_level", "run_level"}:
        return False
    recorded_sum = _semantic_task_source_cap_int(
        runner_source_budget.get("task_max_sources_sum")
    )
    if recorded_sum is not None and recorded_sum != task_max_sources_sum:
        return False
    if task_max_sources_sum > declared_source_budget:
        return runner_source_budget.get("reuse_required") is True
    return True


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


def _question_is_software_implementation_context(text: str) -> bool:
    if not _contains_any(
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
            "testing strategy",
            "\uc544\ud0a4\ud14d\ucc98",
            "\ud14c\uc2a4\ud2b8",
            "\uad6c\ud604",
            "\ucd94\uac00",
        ),
    ):
        return False
    if _contains_any(
        text,
        (
            "codex",
            "deepresearch",
            "semantic planner",
            "runner",
            "fan-out",
            "fanout",
            "api",
            "sdk",
            "software",
            "code",
            "repository",
            "repo",
            "backend",
            "frontend",
            "service architecture",
            "system architecture",
            "runtime",
            "pipeline",
            "module",
            "library",
            "framework",
            "next.js",
            "nextjs",
            "app router",
            "saas",
            "\uc18c\ud504\ud2b8\uc6e8\uc5b4",
            "\ucf54\ub4dc",
            "\ub7f0\ub108",
        ),
    ):
        return True
    return False


def classify_question(question: str) -> str:
    text = question.lower()
    if _mentions_strong_visual_route_evidence(text):
        return _CLASS_VISUAL
    if _question_is_software_implementation_context(text):
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
    if _mentions_explicit_visual_evidence(text):
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
    if artifact.get("ok") is not True:
        _demote_persisted_semantic_release_eligibility(
            run_path=run_path,
            validation=artifact,
        )
    return artifact


def write_semantic_integrity_artifacts(
    *,
    run_dir: str | Path,
    question: str,
    plan: SemanticPlan,
    routing: Sequence[Mapping[str, Any]] | None = None,
    search_tasks: Sequence[Mapping[str, Any]] | None = None,
    created_at: str | None = None,
    locked_oracle: Mapping[str, Any] | None = None,
    semantic_review: Mapping[str, Any] | None = None,
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
    if not plan.raw_request_payload:
        request_payload.setdefault("question", question)
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
    if not plan.raw_response_payload:
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
    oracle_requirement_map = (
        [
            dict(record)
            for record in _list(locked_oracle.get("oracle_requirement_map"))
            if isinstance(record, Mapping)
        ]
        if isinstance(locked_oracle, Mapping)
        else _oracle_requirement_map(plan)
    )
    requirement_coverage_map = _requirement_coverage_map(plan)
    semantic_fit_score = (
        semantic_review.get("semantic_fit_score")
        if isinstance(semantic_review, Mapping)
        else _release_ineligible_semantic_fit_score(plan)
    )
    review_payload = (
        dict(semantic_review)
        if isinstance(semantic_review, Mapping)
        else {
            **base,
            "artifact_type": "semantic_plan_review",
            "semantic_fit_score": semantic_fit_score,
            "blockers": _semantic_review_blockers(plan),
            "warnings": [],
            "reviewer_independence": _reviewer_independence(plan),
            "substitute_implementation_check": _substitute_implementation_check(plan),
            "final_verdict": "release_ineligible",
            "verdict": "release_ineligible",
            "prd_score_dimensions": _score_dimensions({}),
        }
    )
    review_payload.setdefault("schema_version", SEMANTIC_PLANNER_SCHEMA_VERSION)
    review_payload.setdefault("run_id", run_path.name)
    review_payload.setdefault("created_at", timestamp)
    review_payload.setdefault("planner_mode", plan.planner_mode)
    review_payload.setdefault("semantic_release_eligible", plan.semantic_release_eligible)
    review_payload.setdefault("question_scope", base["question_scope"])
    review_payload.setdefault(
        "raw_request_path",
        review_payload.get("reviewer_raw_request_path") or base["raw_request_path"],
    )
    review_payload.setdefault(
        "raw_response_path",
        review_payload.get("reviewer_raw_response_path") or base["raw_response_path"],
    )
    review_payload.setdefault(
        "raw_request_hash",
        review_payload.get("reviewer_raw_request_artifact_hash")
        or review_payload.get("reviewer_raw_request_hash")
        or base["raw_request_hash"],
    )
    review_payload.setdefault(
        "raw_response_hash",
        review_payload.get("reviewer_raw_response_artifact_hash")
        or review_payload.get("reviewer_raw_response_hash")
        or base["raw_response_hash"],
    )
    review_payload.setdefault(
        "raw_request_content_hash",
        review_payload.get("reviewer_raw_request_content_hash"),
    )
    review_payload.setdefault(
        "raw_request_artifact_hash",
        review_payload.get("reviewer_raw_request_artifact_hash")
        or review_payload.get("reviewer_raw_request_hash"),
    )
    review_payload.setdefault(
        "raw_response_artifact_hash",
        review_payload.get("reviewer_raw_response_artifact_hash")
        or review_payload.get("reviewer_raw_response_hash"),
    )
    review_provenance = dict(
        review_payload.get("provenance")
        or review_payload.get("reviewer_provenance")
        or base["provenance"]
    )
    review_provenance.setdefault("planner_mode", plan.planner_mode)
    review_provenance.setdefault("planner_source", plan.source)
    review_provenance.setdefault("raw_request_required", True)
    review_provenance.setdefault("raw_response_required", True)
    review_provenance["semantic_release_eligible"] = plan.semantic_release_eligible
    review_payload.setdefault("reviewer_provenance", review_provenance)
    review_payload["provenance"] = review_provenance
    review_payload.setdefault("template_use", base["template_use"])
    review_payload.setdefault(
        "session_id",
        review_provenance.get("session_id") or review_provenance.get("child_session_id"),
    )
    review_payload.setdefault(
        "session_id_unavailable_reason",
        review_provenance.get("session_id_unavailable_reason"),
    )

    oracle_payload = (
        dict(locked_oracle)
        if isinstance(locked_oracle, Mapping)
        else {
            **base,
            "artifact_type": "semantic_expectation_oracle",
            "oracle_requirement_map": oracle_requirement_map,
            "locked_before_plan_visible": plan.planner_mode == PLANNER_MODE_CODEX_SEMANTIC,
            "reverse_fit_risk": plan.planner_mode != PLANNER_MODE_CODEX_SEMANTIC,
        }
    )
    oracle_payload.setdefault("schema_version", SEMANTIC_PLANNER_SCHEMA_VERSION)
    oracle_payload.setdefault("run_id", run_path.name)
    oracle_payload.setdefault("created_at", timestamp)
    materialization_plan_hash = semantic_materialization_plan_hash_for_tasks(
        plan.bounded_tasks
    )

    artifacts = {
        SEMANTIC_EXPECTATION_ORACLE_FILENAME: oracle_payload,
        SEMANTIC_PLAN_FILENAME: {
            **base,
            "artifact_type": "semantic_plan",
            "semantic_release_eligible": plan.semantic_release_eligible,
            SEMANTIC_MATERIALIZATION_PLAN_HASH_FIELD: materialization_plan_hash,
            "semantic_plan": plan.to_dict(),
            "intent_summary": plan.intent_summary,
            "domain_entities": list(plan.domain_entities),
            "constraints": list(plan.constraints),
            "runner_source_budget": _review_visible_runner_source_budget_metadata(
                plan.runner_source_budget
            ),
            "candidate_question_scope": plan.question_scope,
            "decomposition_strategy": plan.decomposition_strategy,
            "negative_scope": list(plan.negative_scope),
            "bounded_tasks": list(plan.bounded_tasks),
            "planner_provenance": dict(plan.planner_provenance),
            "parsed_response_hash": _planner_provenance(plan).get("parsed_response_hash"),
            "reviewed_candidate_hash": (
                semantic_review.get("semantic_plan_candidate_artifact_hash")
                or semantic_review.get("semantic_plan_candidate_hash")
                if isinstance(semantic_review, Mapping)
                else None
            ),
            "angles": [angle.to_dict() for angle in plan.angles],
            "requirement_coverage_map": requirement_coverage_map,
            "routing_count": len(routing or []),
            "search_task_count": len(search_tasks or []),
        },
        SEMANTIC_PLAN_REVIEW_FILENAME: review_payload,
        SEMANTIC_REQUIREMENT_WAIVERS_FILENAME: {
            **base,
            "artifact_type": "semantic_requirement_waivers",
            "waivers": [],
            "explicit_user_confirmation_required_before_fanout": True,
            "reviewer_acceptance_required": True,
            "status": "no_waivers",
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
            "locked_oracle_hash": (
                locked_oracle.get("oracle_content_hash")
                if isinstance(locked_oracle, Mapping)
                else None
            ),
            "required_trace_events_before_rematerialization": [
                "semantic_delta_request_created",
                "semantic_delta_review_requested",
                "semantic_delta_approved",
                "semantic_plan_rematerialized",
            ],
            "disallowed_repair_categories": list(SEMANTIC_DELTA_DISALLOWED_REPAIR_CATEGORIES),
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
        SEMANTIC_REQUIREMENT_WAIVERS_FILENAME,
        SEMANTIC_PLAN_DELTA_FILENAME,
        SEMANTIC_MATERIALIZATION_DIFF_FILENAME,
    )


def write_semantic_materialization_diff(
    *,
    run_dir: str | Path,
    require_research_tasks: bool = True,
    require_downstream: bool = False,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Write the semantic plan to task-artifact materialization diff."""

    run_path = Path(run_dir)
    diff = build_semantic_materialization_diff(
        run_dir=run_path,
        require_research_tasks=require_research_tasks,
        require_downstream=require_downstream,
        created_at=created_at,
    )
    _write_json(run_path / SEMANTIC_MATERIALIZATION_DIFF_FILENAME, diff)
    return diff


def build_semantic_materialization_diff(
    *,
    run_dir: str | Path,
    require_research_tasks: bool = True,
    require_downstream: bool = False,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Validate exact semantic bounded-task materialization.

    Pre-fanout callers can set ``require_downstream=False`` so search results,
    visual records, and assignment JSONL files are reported but not required.
    Release gates set it to true and therefore require complete downstream
    task-set coverage.
    """

    run_path = Path(run_dir)
    generated_at = created_at or _utc_now_from_run(run_path)
    plan_path = run_path / SEMANTIC_PLAN_FILENAME
    plan_artifact = _read_optional_json(plan_path)
    plan_hash = semantic_materialization_plan_hash_for_artifact(
        plan_artifact,
        fallback_path=plan_path,
    )
    semantic_plan = _semantic_plan_mapping(plan_artifact)
    bounded_tasks = _semantic_plan_bounded_tasks(semantic_plan, plan_artifact)
    plan_task_ids, duplicate_plan_task_ids = _semantic_task_ids_with_duplicates(
        bounded_tasks
    )
    bounded_by_id = {
        task_id: task
        for task_id, task in zip(plan_task_ids, bounded_tasks)
        if task_id and task_id not in duplicate_plan_task_ids
    }
    visual_obligation_task_ids = [
        task_id
        for task_id in plan_task_ids
        if task_id in bounded_by_id
        and _semantic_task_has_visual_obligation(bounded_by_id[task_id])
    ]
    approved_delta = _approved_materialization_delta(run_path)
    approved_delta_id = approved_delta.get("approved_delta_id") or "base_plan"

    failures: list[dict[str, Any]] = []
    if not isinstance(plan_artifact, Mapping):
        failures.append(
            {
                "code": "semantic_plan_missing",
                "artifact": SEMANTIC_PLAN_FILENAME,
            }
        )
    if not bounded_tasks:
        failures.append({"code": "semantic_plan_bounded_tasks_missing"})
    if duplicate_plan_task_ids:
        failures.append(
            {
                "code": "semantic_plan_duplicate_task_ids",
                "task_ids": duplicate_plan_task_ids,
            }
        )

    artifact_checks: list[dict[str, Any]] = []

    research_records, research_present = _read_materialization_json_records(
        run_path / "research_tasks.json"
    )
    artifact_checks.append(
        _materialization_collection_check(
            artifact="research_tasks",
            path=run_path / "research_tasks.json",
            present=research_present,
            records=research_records,
            expected_task_ids=plan_task_ids,
            bounded_by_id=bounded_by_id,
            plan_hash=plan_hash,
            approved_delta_id=approved_delta_id,
            required=require_research_tasks,
            compare_fields=True,
            allow_duplicate_task_ids=False,
        )
    )

    search_records, search_present = _read_materialization_json_records(
        run_path / "search_tasks.json"
    )
    artifact_checks.append(
        _materialization_collection_check(
            artifact="search_tasks",
            path=run_path / "search_tasks.json",
            present=search_present,
            records=search_records,
            expected_task_ids=plan_task_ids,
            bounded_by_id=bounded_by_id,
            plan_hash=plan_hash,
            approved_delta_id=approved_delta_id,
            required=True,
            compare_fields=True,
            allow_duplicate_task_ids=False,
        )
    )

    evidence = _read_optional_json(run_path / "evidence.json")
    evidence_search_tasks = (
        evidence.get("search_tasks")
        if isinstance(evidence, Mapping) and isinstance(evidence.get("search_tasks"), list)
        else []
    )
    artifact_checks.append(
        _materialization_collection_check(
            artifact="evidence.search_tasks",
            path=run_path / "evidence.json",
            present=bool(evidence_search_tasks),
            records=list(evidence_search_tasks),
            expected_task_ids=plan_task_ids,
            bounded_by_id=bounded_by_id,
            plan_hash=plan_hash,
            approved_delta_id=approved_delta_id,
            required=True,
            compare_fields=True,
            allow_duplicate_task_ids=False,
        )
    )

    visual_records, visual_present = _read_materialization_json_records(
        run_path / "visual_tasks.json"
    )
    artifact_checks.append(
        _materialization_collection_check(
            artifact="visual_tasks",
            path=run_path / "visual_tasks.json",
            present=visual_present,
            records=visual_records,
            expected_task_ids=visual_obligation_task_ids,
            bounded_by_id=bounded_by_id,
            plan_hash=plan_hash,
            approved_delta_id=approved_delta_id,
            required=bool(visual_obligation_task_ids),
            compare_fields=True,
            allow_duplicate_task_ids=False,
        )
    )

    visual_plan_records, visual_plan_present = _read_materialization_json_records(
        run_path / "visual_search_plan.json"
    )
    artifact_checks.append(
        _materialization_collection_check(
            artifact="visual_search_plan",
            path=run_path / "visual_search_plan.json",
            present=visual_plan_present,
            records=visual_plan_records,
            expected_task_ids=visual_obligation_task_ids,
            bounded_by_id=bounded_by_id,
            plan_hash=plan_hash,
            approved_delta_id=approved_delta_id,
            required=require_downstream and bool(visual_obligation_task_ids),
            compare_fields=False,
            allow_duplicate_task_ids=False,
        )
    )

    for artifact, filename in SEMANTIC_MATERIALIZATION_JSONL_ARTIFACTS.items():
        records, present = _read_materialization_jsonl_records(run_path / filename)
        expected_ids = (
            visual_obligation_task_ids
            if artifact
            in {"visual_candidates", "image_fetch_status", "visual_observations"}
            else plan_task_ids
        )
        artifact_checks.append(
            _materialization_collection_check(
                artifact=artifact,
                path=run_path / filename,
                present=present,
                records=records,
                expected_task_ids=expected_ids,
                bounded_by_id=bounded_by_id,
                plan_hash=plan_hash,
                approved_delta_id=approved_delta_id,
                required=require_downstream and bool(expected_ids),
                compare_fields=artifact == "search_results",
                compare_field_map=(
                    SEMANTIC_MATERIALIZATION_SEARCH_RESULT_ALIGNMENT_FIELD_MAP
                    if artifact == "search_results"
                    else None
                ),
                require_all_fields=artifact == "search_results",
                allow_duplicate_task_ids=True,
            )
        )

    evidence_images = (
        evidence.get("images")
        if isinstance(evidence, Mapping) and isinstance(evidence.get("images"), list)
        else []
    )
    if evidence_images or (require_downstream and visual_obligation_task_ids):
        artifact_checks.append(
            _materialization_collection_check(
                artifact="evidence.images",
                path=run_path / "evidence.json",
                present=bool(evidence_images),
                records=list(evidence_images),
                expected_task_ids=visual_obligation_task_ids,
                bounded_by_id=bounded_by_id,
                plan_hash=plan_hash,
                approved_delta_id=approved_delta_id,
                required=require_downstream and bool(visual_obligation_task_ids),
                compare_fields=False,
                require_all_fields=False,
                allow_duplicate_task_ids=True,
            )
        )

    missing_task_ids = _unique_sorted(
        task_id
        for check in artifact_checks
        for task_id in check.get("missing_task_ids", [])
        if check.get("required") is True
    )
    extra_task_ids = _unique_sorted(
        task_id
        for check in artifact_checks
        for task_id in check.get("extra_task_ids", [])
    )
    duplicate_task_ids = _unique_sorted(
        [*duplicate_plan_task_ids]
        + [
            task_id
            for check in artifact_checks
            for task_id in check.get("duplicate_semantic_task_ids", [])
        ]
    )
    dropped_search_obligations = _unique_sorted(
        set(plan_task_ids)
        - set(
            task_id
            for check in artifact_checks
            if check.get("artifact") in {"search_tasks", "evidence.search_tasks"}
            for task_id in check.get("materialized_task_ids", [])
        )
    )
    dropped_visual_obligations = _unique_sorted(
        set(visual_obligation_task_ids)
        - set(
            task_id
            for check in artifact_checks
            if check.get("artifact") == "visual_tasks"
            for task_id in check.get("materialized_task_ids", [])
        )
    )
    field_mismatches = [
        mismatch
        for check in artifact_checks
        for mismatch in check.get("field_mismatches", [])
    ]
    lineage_failures = [
        failure
        for check in artifact_checks
        for failure in check.get("lineage_failures", [])
    ]
    missing_required_artifacts = [
        str(check.get("artifact"))
        for check in artifact_checks
        if check.get("required") is True and check.get("present") is not True
    ]
    failed_artifacts = [
        str(check.get("artifact"))
        for check in artifact_checks
        if check.get("valid") is not True and check.get("required") is True
    ]

    suppressible_task_set_differences = (
        missing_task_ids
        or extra_task_ids
        or duplicate_task_ids
        or dropped_search_obligations
        or dropped_visual_obligations
    )
    materialization_differences = (
        suppressible_task_set_differences
        or field_mismatches
        or lineage_failures
        or missing_required_artifacts
    )
    approved_difference = bool(
        suppressible_task_set_differences and approved_delta.get("valid") is True
    )
    if materialization_differences and not approved_difference:
        failures.append(
            {
                "code": "semantic_materialization_difference",
                "missing_task_ids": missing_task_ids,
                "extra_task_ids": extra_task_ids,
                "duplicate_semantic_task_ids": duplicate_task_ids,
                "dropped_search_obligations": dropped_search_obligations,
                "dropped_visual_obligations": dropped_visual_obligations,
                "missing_required_artifacts": missing_required_artifacts,
            }
        )
    if missing_required_artifacts:
        failures.append(
            {
                "code": "semantic_materialization_missing_required_artifacts",
                "artifacts": missing_required_artifacts,
            }
        )
    if field_mismatches:
        failures.append(
            {
                "code": "semantic_materialization_field_mismatch",
                "count": len(field_mismatches),
            }
        )
    if lineage_failures:
        failures.append(
            {
                "code": "semantic_materialization_lineage_failure",
                "count": len(lineage_failures),
            }
        )
    if failed_artifacts and (not approved_difference or missing_required_artifacts):
        failures.append(
            {
                "code": "semantic_materialization_artifact_check_failed",
                "artifacts": failed_artifacts,
            }
        )

    exact_task_set_equality = (
        not missing_task_ids
        and not extra_task_ids
        and not duplicate_task_ids
        and not dropped_search_obligations
        and not dropped_visual_obligations
    )
    valid = not failures
    return {
        "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
        "artifact_type": "semantic_materialization_diff",
        "generated_at": generated_at,
        "run_id": run_path.name,
        "valid": valid,
        "status": "valid" if valid else "failed",
        "full_materialization_validation_implemented": True,
        "require_research_tasks": require_research_tasks,
        "require_downstream": require_downstream,
        "semantic_plan_path": str(plan_path),
        "semantic_plan_hash": plan_hash,
        "approved_delta_id": approved_delta_id,
        "approved_delta": approved_delta,
        "compared_fields": list(SEMANTIC_MATERIALIZATION_ALIGNMENT_FIELDS),
        "search_result_semantic_compared_fields": list(
            SEMANTIC_MATERIALIZATION_SEARCH_RESULT_ALIGNMENT_FIELD_MAP.values()
        ),
        "planned_task_count": len(plan_task_ids),
        "materialized_counts": {
            str(check["artifact"]): check.get("materialized_count", 0)
            for check in artifact_checks
        },
        "planned_task_ids": plan_task_ids,
        "visual_obligation_task_ids": visual_obligation_task_ids,
        "missing_task_ids": missing_task_ids,
        "extra_task_ids": extra_task_ids,
        "duplicate_semantic_task_ids": duplicate_task_ids,
        "dropped_search_obligations": dropped_search_obligations,
        "dropped_visual_obligations": dropped_visual_obligations,
        "missing_required_artifacts": missing_required_artifacts,
        "exact_task_set_equality": exact_task_set_equality,
        "per_artifact_task_set_equality": {
            str(check["artifact"]): check.get("task_set_equal") is True
            for check in artifact_checks
        },
        "field_mismatches": field_mismatches,
        "lineage_failures": lineage_failures,
        "artifact_checks": artifact_checks,
        "failures": failures,
    }


def _semantic_plan_mapping(plan_artifact: Any) -> Mapping[str, Any]:
    if isinstance(plan_artifact, Mapping):
        nested = plan_artifact.get("semantic_plan")
        if isinstance(nested, Mapping):
            return nested
        return plan_artifact
    return {}


def _semantic_plan_bounded_tasks(
    semantic_plan: Mapping[str, Any],
    plan_artifact: Any,
) -> list[Mapping[str, Any]]:
    candidates: list[Any] = []
    if isinstance(semantic_plan.get("bounded_tasks"), list):
        candidates = list(semantic_plan.get("bounded_tasks") or [])
    elif isinstance(plan_artifact, Mapping) and isinstance(
        plan_artifact.get("bounded_tasks"),
        list,
    ):
        candidates = list(plan_artifact.get("bounded_tasks") or [])
    return [task for task in candidates if isinstance(task, Mapping)]


def semantic_materialization_plan_hash_for_tasks(
    tasks: Sequence[Mapping[str, Any]],
) -> str:
    projected_tasks: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        projected: dict[str, Any] = {
            "task_id": str(task.get("task_id") or task.get("id") or ""),
            "angle_id": str(task.get("angle_id") or ""),
        }
        for field in SEMANTIC_MATERIALIZATION_ALIGNMENT_FIELDS:
            projected[field] = _canonical_json_value(task.get(field))
        projected_tasks.append(projected)
    payload = {
        "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
        "artifact_type": "semantic_materialization_plan_hash",
        "bounded_tasks": projected_tasks,
    }
    return _sha256_text(json.dumps(payload, sort_keys=True, ensure_ascii=True))


def _canonical_json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=True))


def semantic_materialization_plan_hash_for_artifact(
    plan_artifact: Any,
    *,
    fallback_path: Path | None = None,
) -> str | None:
    if isinstance(plan_artifact, Mapping):
        value = plan_artifact.get(SEMANTIC_MATERIALIZATION_PLAN_HASH_FIELD)
        if _is_sha256_hex(value):
            return str(value)
        semantic_plan = _semantic_plan_mapping(plan_artifact)
        nested_value = semantic_plan.get(SEMANTIC_MATERIALIZATION_PLAN_HASH_FIELD)
        if _is_sha256_hex(nested_value):
            return str(nested_value)
    if fallback_path is not None and fallback_path.exists():
        return _sha256_file(fallback_path)
    return None


def semantic_materialization_plan_hash_for_file(path: Path) -> str | None:
    payload = _read_optional_json(path)
    return semantic_materialization_plan_hash_for_artifact(payload, fallback_path=path)


def _is_sha256_hex(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _semantic_task_ids_with_duplicates(
    tasks: Sequence[Mapping[str, Any]],
) -> tuple[list[str], list[str]]:
    task_ids: list[str] = []
    counts: Counter[str] = Counter()
    for task in tasks:
        task_id = _semantic_record_task_id(task)
        if task_id:
            task_ids.append(task_id)
            counts[task_id] += 1
    duplicates = sorted(task_id for task_id, count in counts.items() if count > 1)
    return task_ids, duplicates


def _semantic_task_has_visual_obligation(task: Mapping[str, Any]) -> bool:
    route = str(task.get("route") or task.get("modality") or "")
    max_images_value = task.get("max_images")
    max_images_present = max_images_value is not None
    max_images = _nonnegative_int(max_images_value)
    if route == "text_only":
        return False
    if _string_list(task.get("expected_visual_targets")):
        return True
    if _semantic_task_expected_visual_artifacts(task):
        return True
    if max_images > 0:
        return True
    if max_images_present and max_images <= 0:
        return False
    if route == "visual_required":
        return True
    if route == "visual_optional" and not max_images_present:
        return True
    return False


def _semantic_task_expected_visual_artifacts(task: Mapping[str, Any]) -> bool:
    visual_tokens = {
        "image",
        "images",
        "visual",
        "visual_search_plan",
        "visual_candidates",
        "image_fetch_status",
        "visual_observations",
        "vlm_analysis",
        "screenshot",
        "screenshots",
        "chart",
        "charts",
        "diagram",
        "diagrams",
        "figure",
        "figures",
        "photo",
        "photos",
    }
    for field in ("expected_artifacts", "expected_source_types", "expected_evidence"):
        for value in _string_list(task.get(field)):
            normalized = _normalize_text(value).replace("-", "_")
            if normalized in visual_tokens:
                return True
            if any(token in normalized for token in ("image", "visual", "screenshot", "vlm")):
                return True
    evidence_need = str(task.get("evidence_need") or "")
    return evidence_need in VISUAL_EXPECTED_EVIDENCE


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str) and value.strip():
        try:
            return max(0, int(float(value)))
        except ValueError:
            return 0
    return 0


def _read_materialization_json_records(path: Path) -> tuple[list[Mapping[str, Any]], bool]:
    payload = _read_optional_json(path)
    if not isinstance(payload, Mapping):
        return [], path.exists()
    records = payload.get("tasks")
    if not isinstance(records, list):
        records = payload.get("records")
    if not isinstance(records, list):
        records = payload.get("plans")
    if not isinstance(records, list):
        return [], True
    return [record for record in records if isinstance(record, Mapping)], True


def _read_materialization_jsonl_records(path: Path) -> tuple[list[Mapping[str, Any]], bool]:
    if not path.exists():
        return [], False
    records: list[Mapping[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if isinstance(payload, Mapping):
                records.append(payload)
    except (OSError, json.JSONDecodeError):
        return [], True
    return records, True


def _materialization_collection_check(
    *,
    artifact: str,
    path: Path,
    present: bool,
    records: Sequence[Mapping[str, Any]],
    expected_task_ids: Sequence[str],
    bounded_by_id: Mapping[str, Mapping[str, Any]],
    plan_hash: str | None,
    approved_delta_id: str,
    required: bool,
    compare_fields: bool,
    allow_duplicate_task_ids: bool,
    compare_field_map: Mapping[str, str] | None = None,
    require_all_fields: bool = True,
) -> dict[str, Any]:
    expected = list(dict.fromkeys(str(task_id) for task_id in expected_task_ids if task_id))
    materialized_task_ids: list[str] = []
    lineage_failures: list[dict[str, Any]] = []
    field_mismatches: list[dict[str, Any]] = []

    for index, record in enumerate(records, start=1):
        task_id = _semantic_record_task_id(record)
        if not task_id:
            lineage_failures.append(
                {
                    "artifact": artifact,
                    "record_index": index,
                    "code": "semantic_plan_task_id_missing",
                }
            )
            continue
        materialized_task_ids.append(task_id)
        bounded_task = bounded_by_id.get(task_id)
        if not isinstance(bounded_task, Mapping):
            continue
        expected_angle_id = str(bounded_task.get("angle_id") or "")
        actual_angle_id = str(record.get("angle_id") or "")
        if not actual_angle_id:
            lineage_failures.append(
                {
                    "artifact": artifact,
                    "record_index": index,
                    "task_id": task_id,
                    "code": "angle_id_missing",
                    "expected": expected_angle_id,
                    "actual": None,
                }
            )
        elif expected_angle_id and actual_angle_id != expected_angle_id:
            lineage_failures.append(
                {
                    "artifact": artifact,
                    "record_index": index,
                    "task_id": task_id,
                    "code": "angle_id_mismatch",
                    "expected": expected_angle_id,
                    "actual": actual_angle_id,
                }
            )
        actual_plan_hash = record.get("semantic_plan_hash")
        if not isinstance(actual_plan_hash, str) or not actual_plan_hash.strip():
            lineage_failures.append(
                {
                    "artifact": artifact,
                    "record_index": index,
                    "task_id": task_id,
                    "code": "semantic_plan_hash_missing",
                    "expected": plan_hash,
                    "actual": None,
                }
            )
        elif plan_hash and actual_plan_hash.strip() != plan_hash:
            lineage_failures.append(
                {
                    "artifact": artifact,
                    "record_index": index,
                    "task_id": task_id,
                    "code": "semantic_plan_hash_mismatch",
                    "expected": plan_hash,
                    "actual": actual_plan_hash.strip(),
                }
            )
        actual_delta_id = record.get("approved_delta_id")
        if not isinstance(actual_delta_id, str) or not actual_delta_id.strip():
            lineage_failures.append(
                {
                    "artifact": artifact,
                    "record_index": index,
                    "task_id": task_id,
                    "code": "approved_delta_id_missing",
                    "expected": approved_delta_id,
                    "actual": None,
                }
            )
        elif actual_delta_id.strip() != approved_delta_id:
            lineage_failures.append(
                {
                    "artifact": artifact,
                    "record_index": index,
                    "task_id": task_id,
                    "code": "approved_delta_id_mismatch",
                    "expected": approved_delta_id,
                    "actual": actual_delta_id.strip(),
                }
            )
        if compare_fields:
            field_mismatches.extend(
                _semantic_materialization_field_mismatches(
                    artifact=artifact,
                    record_index=index,
                    task_id=task_id,
                    expected=bounded_task,
                    actual=record,
                    field_map=compare_field_map,
                    require_all_fields=require_all_fields,
                )
            )

    counts = Counter(materialized_task_ids)
    duplicate_task_ids = (
        []
        if allow_duplicate_task_ids
        else sorted(task_id for task_id, count in counts.items() if count > 1)
    )
    unique_materialized = list(dict.fromkeys(materialized_task_ids))
    missing = sorted(set(expected) - set(unique_materialized))
    extra = sorted(set(unique_materialized) - set(expected))
    task_set_equal = not missing and not extra and not duplicate_task_ids
    if required and not present:
        task_set_equal = False
    valid = (
        (not required or present)
        and task_set_equal
        and not field_mismatches
        and not lineage_failures
    )
    return {
        "artifact": artifact,
        "path": str(path),
        "required": required,
        "present": present,
        "valid": valid,
        "task_set_equal": task_set_equal,
        "planned_task_ids": expected,
        "materialized_task_ids": unique_materialized,
        "planned_count": len(expected),
        "materialized_count": len(unique_materialized),
        "record_count": len(records),
        "missing_task_ids": missing,
        "extra_task_ids": extra,
        "duplicate_semantic_task_ids": duplicate_task_ids,
        "field_mismatches": field_mismatches,
        "compared_fields": (
            list((compare_field_map or {}).values())
            if compare_fields and compare_field_map
            else list(SEMANTIC_MATERIALIZATION_ALIGNMENT_FIELDS)
            if compare_fields
            else []
        ),
        "lineage_failures": lineage_failures,
        "lineage_join_fields": [
            "semantic_plan_hash",
            "semantic_plan_task_id",
            "angle_id",
            "approved_delta_id",
        ],
        "joinable_lineage": {
            "semantic_plan_hash": plan_hash,
            "approved_delta_id": approved_delta_id,
            "task_id_join": bool(plan_hash and bounded_by_id),
        },
    }


def _semantic_record_task_id(record: Mapping[str, Any]) -> str:
    for field in ("semantic_plan_task_id", "task_id", "search_task_id", "id"):
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _semantic_materialization_field_mismatches(
    *,
    artifact: str,
    record_index: int,
    task_id: str,
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    field_map: Mapping[str, str] | None = None,
    require_all_fields: bool = True,
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for field in SEMANTIC_MATERIALIZATION_ALIGNMENT_FIELDS:
        actual_field = field_map.get(field, field) if field_map else field
        if actual_field not in actual:
            if not require_all_fields:
                continue
            mismatches.append(
                {
                    "artifact": artifact,
                    "record_index": record_index,
                    "task_id": task_id,
                    "field": actual_field,
                    "semantic_field": field,
                    "code": "field_missing",
                    "expected": _semantic_materialization_value(expected, field),
                    "actual": None,
                }
            )
            continue
        expected_value = _semantic_materialization_value(expected, field)
        actual_value = _semantic_materialization_value(
            actual,
            actual_field,
            semantic_field=field,
        )
        if expected_value != actual_value:
            mismatches.append(
                {
                    "artifact": artifact,
                    "record_index": record_index,
                    "task_id": task_id,
                    "field": actual_field,
                    "semantic_field": field,
                    "code": "field_mismatch",
                    "expected": expected_value,
                    "actual": actual_value,
                }
            )
    return mismatches


def _semantic_materialization_value(
    record: Mapping[str, Any],
    field: str,
    *,
    semantic_field: str | None = None,
) -> Any:
    comparison_field = semantic_field or field
    value = record.get(field)
    if comparison_field in {
        "expected_source_types",
        "expected_visual_targets",
        "expected_artifacts",
        "success_criteria",
    }:
        return _string_list(value)
    if comparison_field == "source_policy":
        return _normalize_mapping_value(value)
    if comparison_field in {"max_results", "max_sources", "max_images"}:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0
    if comparison_field in {"query", "route", "freshness_requirement", "done_condition"}:
        return str(value or "")
    return value


def _normalize_mapping_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=True))


def _unique_sorted(values: Any) -> list[str]:
    return sorted({str(value) for value in values if str(value)})


def _approved_materialization_delta(run_path: Path) -> dict[str, Any]:
    delta = _read_optional_json(run_path / SEMANTIC_PLAN_DELTA_FILENAME)
    if not isinstance(delta, Mapping) or delta.get("delta_applied") is not True:
        return {
            "valid": False,
            "approved_delta_id": "base_plan",
            "delta_applied": False,
            "failures": [],
        }
    failures: list[str] = []
    approved_delta_id = str(
        delta.get("approved_delta_id")
        or delta.get("delta_id")
        or delta.get("new_semantic_plan_version_id")
        or ""
    ).strip()
    if not approved_delta_id:
        failures.append("approved_delta_id_missing")
    if delta.get("reviewer_approved") is not True:
        failures.append("reviewer_approved_missing")
    if not (
        delta.get("created_before_fanout") is True
        or delta.get("approved_before_fanout") is True
        or delta.get("semantic_plan_rematerialized_before_fanout") is True
    ):
        failures.append("approved_delta_not_recorded_before_fanout")
    repair_categories = set(_string_list(delta.get("repair_categories")))
    disallowed = repair_categories & set(SEMANTIC_DELTA_DISALLOWED_REPAIR_CATEGORIES)
    if disallowed:
        failures.append("disallowed_repair_categories:" + ",".join(sorted(disallowed)))
    return {
        "valid": not failures,
        "approved_delta_id": approved_delta_id or "unapproved_delta",
        "delta_applied": True,
        "failures": failures,
    }


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
    broad_question = _effective_broad_question(
        question_class=question_class,
        expected_needs=expected_needs,
        declared_broad=bool(planner_metadata.get("broad_question")),
    )

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
    declared_semantic_release_eligible = bool(
        planner_metadata.get("semantic_release_eligible")
    )
    semantic_status = _semantic_release_status(
        run_path=run_path,
        planner_metadata=planner_metadata,
    )
    failures.extend(semantic_status["failures"])

    covered_needs = [
        need for need in expected_needs if need in evidence_need_counts
    ]
    semantic_release_eligible = declared_semantic_release_eligible and not failures
    semantic_status = dict(semantic_status)
    semantic_status["semantic_release_eligible"] = semantic_release_eligible
    semantic_status["declared_semantic_release_eligible"] = (
        declared_semantic_release_eligible
    )
    artifact = {
        "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
        "fixture_id": str(planner_metadata.get("fixture_id") or run_path.name),
        "run_id": str(evidence.get("run_id") or run_path.name),
        "planner_mode": planner_mode or "unknown",
        "semantic_release_eligible": semantic_release_eligible,
        "declared_semantic_release_eligible": declared_semantic_release_eligible,
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


def _demote_persisted_semantic_release_eligibility(
    *,
    run_path: Path,
    validation: Mapping[str, Any],
) -> None:
    failure_codes = [
        str(failure.get("code"))
        for failure in _list(validation.get("failures"))
        if isinstance(failure, Mapping) and failure.get("code")
    ]
    demotion = {
        "semantic_release_eligible": False,
        "semantic_release_ineligible_reason": "semantic_planner_validation_failed",
        "semantic_planner_validation_ok": False,
        "semantic_planner_validation_failure_codes": failure_codes,
    }
    _demote_json_artifact_semantic_release(run_path / "evidence.json", demotion)
    _demote_json_artifact_semantic_release(run_path / SEMANTIC_PLAN_FILENAME, demotion)
    _demote_json_artifact_semantic_release(run_path / "status.json", demotion)


def _demote_json_artifact_semantic_release(path: Path, demotion: Mapping[str, Any]) -> None:
    payload = _read_optional_json(path)
    if not isinstance(payload, dict):
        return
    changed = False
    for key, value in demotion.items():
        if payload.get(key) != value:
            payload[key] = copy.deepcopy(value)
            changed = True
    semantic_planner = payload.get("semantic_planner")
    if isinstance(semantic_planner, dict):
        for key, value in demotion.items():
            if semantic_planner.get(key) != value:
                semantic_planner[key] = copy.deepcopy(value)
                changed = True
    semantic_plan = payload.get("semantic_plan")
    if isinstance(semantic_plan, dict):
        for key, value in demotion.items():
            if semantic_plan.get(key) != value:
                semantic_plan[key] = copy.deepcopy(value)
                changed = True
    semantic_planning = payload.get("semantic_planning")
    if isinstance(semantic_planning, dict):
        if semantic_planning.get("semantic_release_eligible") is not False:
            semantic_planning["semantic_release_eligible"] = False
            changed = True
        if semantic_planning.get("validation_ok") is not False:
            semantic_planning["validation_ok"] = False
            changed = True
        failure_codes = list(demotion.get("semantic_planner_validation_failure_codes") or [])
        if semantic_planning.get("failure_codes") != failure_codes:
            semantic_planning["failure_codes"] = failure_codes
            changed = True
    if changed:
        _write_json(path, payload)


def question_mentions_visual_evidence(question: str) -> bool:
    return _mentions_explicit_visual_evidence(question.lower())


def _mentions_explicit_visual_evidence(text: str) -> bool:
    return bool(
        re.search(
            r"\b("
            r"visual|ui|screenshot(s)?|screen(s)?|interface(s)?|chart(s)?|"
            r"graph(s)?|diagram(s)?|image(s)?|photo(s)?|"
            r"image[- ](quality|evidence|comparison|source(s)?|artifact(s)?)|"
            r"visual[- ](evidence|comparison|inspection|analysis|observation(s)?|example(s)?|style)"
            r")\b",
            text,
        )
        or _contains_any(
            text,
            ("\uc0ac\uc9c4", "\uc774\ubbf8\uc9c0", "\uc2a4\ub0c5\uc0ac\uc9c4", "\uc2dc\uac01", "\ud654\uba74", "\ucc28\ud2b8"),
        )
    )


def _mentions_strong_visual_route_evidence(text: str) -> bool:
    return bool(
        re.search(
            r"\b("
            r"ui|screenshot(s)?|screen(s)?|interface(s)?|chart(s)?|"
            r"graph(s)?|diagram(s)?|"
            r"image[- ](quality|evidence|comparison|source(s)?|artifact(s)?)|"
            r"visual[- ](evidence|comparison|inspection|analysis|observation(s)?|example(s)?|style)"
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
    provenance = _planner_provenance(plan)
    provenance.setdefault("planner_mode", plan.planner_mode)
    provenance.setdefault("planner_source", plan.source)
    provenance.setdefault("raw_request_required", True)
    provenance.setdefault("raw_response_required", True)
    provenance["semantic_release_eligible"] = plan.semantic_release_eligible
    return {
        "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
        "run_id": run_path.name,
        "created_at": created_at,
        "planner_mode": plan.planner_mode,
        "semantic_release_eligible": plan.semantic_release_eligible,
        "status": plan.status,
        "question_scope": _question_scope(question, plan),
        "raw_request_path": str(raw_request_path),
        "raw_response_path": str(raw_response_path),
        "raw_request_hash": raw_request_hash,
        "raw_response_hash": raw_response_hash,
        "parsed_response_hash": provenance.get("parsed_response_hash"),
        "provenance": provenance,
        "template_use": _template_use(plan),
        "session_id": provenance.get("session_id") or provenance.get("child_session_id"),
        "session_id_unavailable_reason": provenance.get("session_id_unavailable_reason"),
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
        plan_artifact = _read_optional_json(run_path / SEMANTIC_PLAN_FILENAME)
        if not isinstance(plan_artifact, Mapping):
            failures.append({"code": "semantic_plan_artifact_missing"})
        else:
            planner_provenance = plan_artifact.get("planner_provenance") or plan_artifact.get(
                "provenance"
            )
            if not isinstance(planner_provenance, Mapping):
                failures.append({"code": "semantic_planner_provenance_missing"})
            elif not _provenance_release_identity_set(planner_provenance):
                failures.append({"code": "semantic_planner_provenance_incomplete"})
            if _provenance_is_non_release_fixture(planner_provenance):
                failures.append({"code": "semantic_planner_non_release_fixture"})
            failures.extend(
                _semantic_raw_artifact_failures(
                    run_path=run_path,
                    artifact_label="planner",
                    request_path=plan_artifact.get("raw_request_path"),
                    response_path=plan_artifact.get("raw_response_path"),
                    request_hash=(
                        plan_artifact.get("raw_request_artifact_hash")
                        or plan_artifact.get("raw_request_hash")
                    ),
                    response_hash=(
                        plan_artifact.get("raw_response_artifact_hash")
                        or plan_artifact.get("raw_response_hash")
                    ),
                    request_content_hash=plan_artifact.get("raw_request_content_hash"),
                )
            )
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
        if not isinstance(review, Mapping):
            failures.append({"code": "semantic_plan_review_missing"})
        else:
            if review.get("verdict") != "pass" and review.get("final_verdict") != "pass":
                failures.append({"code": "semantic_review_verdict_not_pass"})
            if review.get("non_negotiable_coverage_complete") is not True:
                failures.append({"code": "non_negotiable_coverage_incomplete"})
            independence = review.get("reviewer_independence")
            if not isinstance(independence, Mapping) or independence.get("independent") is not True:
                failures.append({"code": "reviewer_independence_failed"})
            reviewer_provenance = review.get("reviewer_provenance") or review.get(
                "provenance"
            )
            if not isinstance(reviewer_provenance, Mapping):
                failures.append({"code": "reviewer_provenance_missing"})
            elif not _provenance_has_required_raw_artifacts(reviewer_provenance):
                failures.append({"code": "reviewer_provenance_incomplete"})
            if _provenance_is_non_release_fixture(reviewer_provenance):
                failures.append({"code": "semantic_reviewer_non_release_fixture"})
            if not str(review.get("reviewer_raw_request_path") or "").strip():
                failures.append({"code": "reviewer_raw_request_path_missing"})
            if not str(review.get("reviewer_raw_response_path") or "").strip():
                failures.append({"code": "reviewer_raw_response_path_missing"})
            if not str(review.get("oracle_hash") or review.get("semantic_expectation_oracle_hash") or "").strip():
                failures.append({"code": "review_oracle_hash_missing"})
            failures.extend(
                _semantic_raw_artifact_failures(
                    run_path=run_path,
                    artifact_label="reviewer",
                    request_path=review.get("reviewer_raw_request_path"),
                    response_path=review.get("reviewer_raw_response_path"),
                    request_hash=(
                        review.get("reviewer_raw_request_artifact_hash")
                        or review.get("reviewer_raw_request_hash")
                    ),
                    response_hash=(
                        review.get("reviewer_raw_response_artifact_hash")
                        or review.get("reviewer_raw_response_hash")
                    ),
                    request_content_hash=review.get("reviewer_raw_request_content_hash"),
                )
            )
        failures.extend(_semantic_oracle_release_failures(run_path))
        failures.extend(_semantic_ordering_proof_failures(run_path))
        failures.extend(_semantic_waiver_release_failures(run_path))
        failures.extend(_semantic_delta_release_failures(run_path))
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
        "review_verdict": (
            review.get("verdict") or review.get("final_verdict")
            if isinstance(review, Mapping)
            else None
        ),
        "review_blocker_count": len(blockers),
        "substitute_implementation_check_passed": substitute_check.get("passed"),
        "failures": failures,
    }


def _semantic_oracle_release_failures(run_path: Path) -> list[dict[str, Any]]:
    oracle = _read_optional_json(run_path / SEMANTIC_EXPECTATION_ORACLE_FILENAME)
    failures: list[dict[str, Any]] = []
    if not isinstance(oracle, Mapping):
        return [{"code": "semantic_expectation_oracle_missing"}]
    for field in _semantic_oracle_required_fields():
        if field not in oracle:
            failures.append({"code": "semantic_oracle_missing_required_field", "field": field})
    requirement_map = oracle.get("oracle_requirement_map")
    if not isinstance(requirement_map, list) or not requirement_map:
        failures.append({"code": "oracle_requirement_map_missing_or_empty"})
    for field in (
        "plan_visible_to_oracle",
        "used_production_planner_output",
        "used_hidden_template_class",
        "used_fixed_angle_inventory",
    ):
        if oracle.get(field) is not False:
            failures.append({"code": "semantic_oracle_reverse_fit_field_invalid", "field": field})
    provenance = oracle.get("oracle_provenance") or oracle.get("provenance")
    if not isinstance(provenance, Mapping):
        failures.append({"code": "semantic_oracle_provenance_missing"})
    elif not _provenance_has_required_raw_artifacts(provenance):
        failures.append({"code": "semantic_oracle_provenance_incomplete"})
    if isinstance(provenance, Mapping):
        failures.extend(
            _semantic_raw_artifact_failures(
                run_path=run_path,
                artifact_label="oracle",
                request_path=provenance.get("raw_request_path") or oracle.get("raw_request_path"),
                response_path=provenance.get("raw_response_path") or oracle.get("raw_response_path"),
                request_hash=(
                    provenance.get("raw_request_artifact_hash")
                    or oracle.get("raw_request_artifact_hash")
                    or provenance.get("raw_request_hash")
                    or oracle.get("raw_request_hash")
                ),
                response_hash=(
                    provenance.get("raw_response_artifact_hash")
                    or oracle.get("raw_response_artifact_hash")
                    or provenance.get("raw_response_hash")
                    or oracle.get("raw_response_hash")
                ),
                request_content_hash=(
                    provenance.get("raw_request_content_hash")
                    or oracle.get("raw_request_content_hash")
                ),
            )
        )
    if _provenance_is_non_release_fixture(provenance):
        failures.append({"code": "semantic_oracle_non_release_fixture"})
    return failures


def _semantic_raw_artifact_failures(
    *,
    run_path: Path,
    artifact_label: str,
    request_path: Any,
    response_path: Any,
    request_hash: Any,
    response_hash: Any,
    request_content_hash: Any = None,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    failures.extend(
        _semantic_file_hash_failures(
            run_path=run_path,
            artifact_label=f"{artifact_label}_raw_request",
            path_value=request_path,
            expected_hash=request_hash,
        )
    )
    failures.extend(
        _semantic_file_hash_failures(
            run_path=run_path,
            artifact_label=f"{artifact_label}_raw_response",
            path_value=response_path,
            expected_hash=response_hash,
        )
    )
    if request_content_hash:
        request_artifact_path = _semantic_artifact_path(run_path, request_path)
        if request_artifact_path is not None and request_artifact_path.exists():
            payload = _read_optional_json(request_artifact_path)
            if isinstance(payload, Mapping):
                content_payload = dict(payload)
                content_payload.pop("raw_request_content_hash", None)
                content_payload.pop("raw_request_hash", None)
                actual_content_hash = _sha256_payload(content_payload)
                if actual_content_hash != str(request_content_hash):
                    failures.append(
                        {
                            "code": f"{artifact_label}_raw_request_content_hash_mismatch",
                            "expected_hash": str(request_content_hash),
                            "actual_hash": actual_content_hash,
                        }
                    )
    return failures


def _semantic_file_hash_failures(
    *,
    run_path: Path,
    artifact_label: str,
    path_value: Any,
    expected_hash: Any,
) -> list[dict[str, Any]]:
    path = _semantic_artifact_path(run_path, path_value)
    if path is None:
        return [{"code": f"{artifact_label}_path_missing"}]
    if not path.exists():
        return [
            {
                "code": f"{artifact_label}_artifact_missing",
                "path": str(path),
            }
        ]
    expected = str(expected_hash or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return [{"code": f"{artifact_label}_hash_missing_or_invalid"}]
    actual = _sha256_file(path)
    if actual != expected:
        return [
            {
                "code": f"{artifact_label}_hash_mismatch",
                "path": str(path),
                "expected_hash": expected,
                "actual_hash": actual,
            }
        ]
    return []


def _semantic_artifact_path(run_path: Path, path_value: Any) -> Path | None:
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    path = Path(path_value)
    if not path.is_absolute():
        path = run_path / path
    return path


def _semantic_ordering_proof_failures(run_path: Path) -> list[dict[str, Any]]:
    trace_file = run_path / "run_trace.jsonl"
    if not trace_file.exists():
        return [{"code": "semantic_ordering_trace_missing"}]
    try:
        records = [
            json.loads(line)
            for line in trace_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except json.JSONDecodeError:
        return [{"code": "semantic_ordering_trace_invalid_json"}]
    required = [
        "semantic_oracle_request_created",
        "semantic_oracle_locked",
        "semantic_planner_request_created",
        "semantic_plan_created",
        "semantic_reviewer_request_created",
        "semantic_review_completed",
    ]
    expected_semantic_indexes = {
        event: index for index, event in enumerate(required, start=1)
    }
    first_by_event: dict[str, tuple[int, Mapping[str, Any]]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            continue
        event_type = str(record.get("event_type") or "")
        if event_type in required and event_type not in first_by_event:
            first_by_event[event_type] = (index, record)
    failures: list[dict[str, Any]] = []
    missing = [event for event in required if event not in first_by_event]
    if missing:
        failures.append({"code": "semantic_ordering_events_missing", "missing_events": missing})
        return failures
    indexes = [first_by_event[event][0] for event in required]
    if indexes != sorted(indexes):
        failures.append(
            {
                "code": "semantic_ordering_events_out_of_order",
                "event_indexes": dict(zip(required, indexes)),
            }
        )
    semantic_indexes: list[int] = []
    for event in required:
        record = first_by_event[event][1]
        semantic_index = record.get("semantic_event_index")
        order_validation = record.get("order_validation")
        order_index = (
            order_validation.get("semantic_event_index")
            if isinstance(order_validation, Mapping)
            else None
        )
        order_required = (
            order_validation.get("required_order")
            if isinstance(order_validation, Mapping)
            else None
        )
        order_current_event = (
            order_validation.get("current_event")
            if isinstance(order_validation, Mapping)
            else None
        )
        expected_index = expected_semantic_indexes[event]
        if order_required != required or order_current_event != event:
            failures.append(
                {
                    "code": "semantic_ordering_order_validation_invalid",
                    "event": event,
                }
            )
        if (
            not isinstance(semantic_index, int)
            or isinstance(semantic_index, bool)
            or semantic_index != expected_index
        ):
            failures.append(
                {
                    "code": "semantic_ordering_event_index_invalid",
                    "event": event,
                    "semantic_event_index": semantic_index,
                    "expected_semantic_event_index": expected_index,
                }
            )
        else:
            semantic_indexes.append(semantic_index)
        if order_index != semantic_index:
            failures.append(
                {
                    "code": "semantic_ordering_event_index_mismatch",
                    "event": event,
                    "semantic_event_index": semantic_index,
                    "order_validation_index": order_index,
                }
            )
    if len(semantic_indexes) == len(required) and semantic_indexes != sorted(semantic_indexes):
        failures.append(
            {
                "code": "semantic_ordering_event_indexes_not_monotonic",
                "semantic_event_indexes": dict(zip(required, semantic_indexes)),
            }
        )
    semantic_timestamps: list[datetime] = []
    raw_semantic_timestamps: dict[str, Any] = {}
    for event in required:
        record = first_by_event[event][1]
        raw_timestamp = record.get("timestamp")
        raw_semantic_timestamps[event] = raw_timestamp
        parsed_timestamp = _parse_semantic_trace_timestamp(raw_timestamp)
        if parsed_timestamp is None:
            failures.append(
                {
                    "code": "semantic_ordering_event_timestamp_invalid",
                    "event": event,
                    "timestamp": raw_timestamp,
                }
            )
        else:
            semantic_timestamps.append(parsed_timestamp)
    if len(semantic_timestamps) == len(required):
        for previous, current in zip(semantic_timestamps, semantic_timestamps[1:]):
            if not previous < current:
                failures.append(
                    {
                        "code": "semantic_ordering_event_timestamps_not_strictly_increasing",
                        "semantic_event_timestamps": raw_semantic_timestamps,
                    }
                )
                break
    for event in required:
        record = first_by_event[event][1]
        artifact_hashes = record.get("artifact_hashes")
        if not isinstance(artifact_hashes, Mapping) or not artifact_hashes:
            failures.append({"code": "semantic_ordering_event_missing_hashes", "event": event})
            continue
        artifact_paths = record.get("semantic_artifact_paths")
        if not isinstance(artifact_paths, Mapping):
            failures.append(
                {"code": "semantic_ordering_event_missing_artifact_paths", "event": event}
            )
            continue
        for key, expected_hash in artifact_hashes.items():
            raw_path = artifact_paths.get(key)
            if not isinstance(raw_path, str) or not raw_path:
                failures.append(
                    {
                        "code": "semantic_ordering_artifact_path_missing",
                        "event": event,
                        "artifact": key,
                    }
                )
                continue
            path = Path(raw_path)
            if not path.is_absolute():
                path = run_path / path
            if not path.exists():
                failures.append(
                    {
                        "code": "semantic_ordering_artifact_missing",
                        "event": event,
                        "artifact": key,
                        "path": str(path),
                    }
                )
                continue
            if _sha256_file(path) != expected_hash:
                if (
                    event == "semantic_plan_created"
                    and key == "semantic_plan"
                    and _semantic_plan_reviewed_candidate_hash(path) == expected_hash
                ):
                    continue
                failures.append(
                    {
                        "code": "semantic_ordering_hash_mismatch",
                        "event": event,
                        "artifact": key,
                    }
                )
    return failures


def _semantic_plan_reviewed_candidate_hash(path: Path) -> str | None:
    payload = _read_optional_json(path)
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("reviewed_candidate_hash")
    return str(value) if value else None


def _parse_semantic_trace_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _semantic_waiver_release_failures(run_path: Path) -> list[dict[str, Any]]:
    path = run_path / SEMANTIC_REQUIREMENT_WAIVERS_FILENAME
    if not path.exists():
        return []
    payload = _read_optional_json(path)
    if not isinstance(payload, Mapping):
        return [{"code": "semantic_requirement_waivers_invalid"}]
    waivers = payload.get("waivers")
    if not isinstance(waivers, list) or not waivers:
        return []
    failures = []
    for index, waiver in enumerate(waivers, start=1):
        if not isinstance(waiver, Mapping):
            failures.append({"code": "semantic_requirement_waiver_not_object", "index": index})
            continue
        if waiver.get("explicit_user_confirmation") is not True:
            failures.append({"code": "semantic_requirement_waiver_missing_user_confirmation", "index": index})
        if waiver.get("reviewer_accepted") is not True:
            failures.append({"code": "semantic_requirement_waiver_missing_reviewer_acceptance", "index": index})
    return failures


def _semantic_delta_release_failures(run_path: Path) -> list[dict[str, Any]]:
    delta = _read_optional_json(run_path / SEMANTIC_PLAN_DELTA_FILENAME)
    if not isinstance(delta, Mapping) or delta.get("delta_applied") is not True:
        return []
    failures: list[dict[str, Any]] = []
    repair_categories = set(_string_list(delta.get("repair_categories")))
    disallowed = repair_categories & set(SEMANTIC_DELTA_DISALLOWED_REPAIR_CATEGORIES)
    if disallowed:
        failures.append(
            {
                "code": "semantic_delta_disallowed_repair_category",
                "repair_categories": sorted(disallowed),
            }
        )
    if delta.get("reviewer_approved") is not True:
        failures.append({"code": "semantic_delta_reviewer_approval_missing"})
    if not str(delta.get("locked_oracle_hash") or "").strip():
        failures.append({"code": "semantic_delta_locked_oracle_hash_missing"})
    trace_failures = _semantic_delta_trace_failures(run_path)
    failures.extend(trace_failures)
    return failures


def _semantic_delta_trace_failures(run_path: Path) -> list[dict[str, Any]]:
    trace_file = run_path / "run_trace.jsonl"
    if not trace_file.exists():
        return [{"code": "semantic_delta_trace_missing"}]
    try:
        event_types = [
            str(json.loads(line).get("event_type") or "")
            for line in trace_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except json.JSONDecodeError:
        return [{"code": "semantic_delta_trace_invalid_json"}]
    required = [
        "semantic_delta_request_created",
        "semantic_delta_review_requested",
        "semantic_delta_approved",
        "semantic_plan_rematerialized",
    ]
    missing = [event for event in required if event not in event_types]
    return (
        [{"code": "semantic_delta_trace_events_missing", "missing_events": missing}]
        if missing
        else []
    )


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
