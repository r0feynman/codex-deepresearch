"""Deterministic semantic angle planning and validation."""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from dataclasses import asdict, dataclass
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
    planner_mode: str = PLANNER_MODE_HEURISTIC_TEMPLATE_FALLBACK
    semantic_release_eligible: bool = False
    status: str = "prepared_heuristic_template_fallback"
    diagnostics: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["angles"] = [angle.to_dict() for angle in self.angles]
        data["diagnostics"] = dict(self.diagnostics or {})
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
        planner_mode=PLANNER_MODE_BLOCKED,
        semantic_release_eligible=False,
        status=BLOCKED_SEMANTIC_PLANNER_UNAVAILABLE,
        diagnostics={
            **_fallback_diagnostics(PLANNER_MODE_BLOCKED),
            "blocked_reason": reason,
        },
    )


def plan_semantic_angles(
    *,
    question: str,
    explicit_angles: Sequence[str] | None = None,
) -> SemanticPlan:
    """Compatibility wrapper returning release-ineligible fallback plans."""

    if explicit_angles is not None:
        return manual_angle_planner(question=question, explicit_angles=explicit_angles)
    return blocked_semantic_planner_plan(
        question=question,
        reason=(
            "Codex-native semantic planner is not implemented yet; refusing to "
            "materialize heuristic template tasks as semantic decomposition."
        ),
    )


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

    request_payload = {
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
    response_payload = {
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
        "provenance": _planner_provenance(plan),
        "template_use": _template_use(plan),
        "session_id": None,
        "session_id_unavailable_reason": _planner_provenance(plan)[
            "session_id_unavailable_reason"
        ],
    }


def _oracle_requirement_map(plan: SemanticPlan) -> list[dict[str, Any]]:
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
            numeric_score = -1.0
        if numeric_score < SEMANTIC_FIT_SCORE_THRESHOLD:
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
