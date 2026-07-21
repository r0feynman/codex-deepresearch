#!/usr/bin/env python3
"""Deterministic semantic adapter for prepare-path scope downgrade validation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_semantic_planner import SemanticPlannerTests  # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 2 or args[0] != "exec" or "--json" not in args[1:]:
        print("semantic downgrade fixture expected codex exec --json", file=sys.stderr)
        return 2

    request = json.loads(sys.stdin.read())
    helper = SemanticPlannerTests(methodName="runTest")
    artifact_type = str(request.get("artifact_type") or "")
    target_scope = os.environ.get("CODEX_DEEPRESEARCH_TEST_SCOPE_DOWNGRADE", "medium")
    if target_scope not in {"medium", "narrow"}:
        target_scope = "medium"

    if artifact_type == "semantic_oracle_raw_request":
        response = helper.oracle_adapter_response(
            request,
            question_scope="broad",
            requirement_types=("subject", "source_quality", "deliverable_shape"),
        )
    elif artifact_type == "semantic_reviewer_raw_request":
        response = helper.reviewer_adapter_response(request)
    else:
        if target_scope == "narrow":
            response = helper.codex_adapter_response(
                request,
                question_scope="narrow",
                angle_count=4,
                tasks_per_angle=2,
                requirement_types=("subject", "source_quality", "visual_modality"),
                visual_angle_indexes=(2, 3),
            )
            if str(request.get("visual_preference") or "") == "visual_optional":
                _sanitize_narrow_visual_optional_candidate(response["candidate_plan"])
        else:
            response = helper.codex_adapter_response(
                request,
                question_scope="medium",
                angle_count=5,
                tasks_per_angle=2,
                requirement_types=("subject", "source_quality", "deliverable_shape"),
            )
            _sanitize_medium_text_only_candidate(response["candidate_plan"])

    print(json.dumps(response, ensure_ascii=False, sort_keys=True))
    return 0


def _sanitize_medium_text_only_candidate(candidate: dict) -> None:
    question = str(candidate.get("original_question") or "")
    angle_specs = [
        (
            "primary_source",
            "Adopted Planning Metric Baseline",
            "Which adopted local plans define measurable implementation indicators?",
            "adopted planning metric inventory",
        ),
        (
            "official_source",
            "Responsible Agency Assignments",
            "Which official plan sections assign lead and partner agencies?",
            "agency responsibility matrix",
        ),
        (
            "recent_change",
            "Plan Update And Effective Period",
            "Which plan dates, amendments, and target years bound each implementation claim?",
            "plan update timeline",
        ),
        (
            "comparative_analysis",
            "Cross-Government Indicator Differences",
            "How do indicator definitions and responsible agencies differ across governments?",
            "cross-government comparison table",
        ),
        (
            "counter_evidence",
            "Responsibility Gaps And Caveats",
            "Where do official records omit owners or create overlapping responsibility?",
            "responsibility gap register",
        ),
    ]
    for index, angle in enumerate(candidate.get("angles", []), start=1):
        need, title, research_question, artifact = angle_specs[(index - 1) % len(angle_specs)]
        angle["route"] = "text_only"
        angle["evidence_need"] = need
        angle["title"] = title
        angle["research_question"] = f"{research_question} Scope: {question}."
        angle["why_this_angle_matters"] = "This preserves a distinct official planning evidence need."
        angle["included_scope"] = [question]
        angle["excluded_scope"] = ["Do not add software implementation, image, chart, or VLM work."]
        angle["expected_source_types"] = ["official local government plans", "official implementation reports"]
        angle["expected_visual_targets"] = []
        angle["expected_artifacts"] = [artifact, "official source notes"]
        angle["search_queries"] = [f"{question} {need} official local government plan"]
        angle["success_criteria"] = [
            "Use official local government planning records.",
            "Record indicators, agencies, caveats, and unknowns.",
        ]
        angle["report_section"] = f"Planning Evidence {index}"
        angle["risk_or_contradiction_checks"] = [
            "Check stale plan years and conflicting responsibility assignments."
        ]
    for index, task in enumerate(candidate.get("bounded_tasks", []), start=1):
        spec = angle_specs[(index - 1) % len(angle_specs)]
        task["route"] = "text_only"
        task["query"] = f"{question} {spec[1]} official planning records task {index}"
        task["expected_visual_targets"] = []
        task["expected_artifacts"] = [spec[3], "official source notes"]
        task["success_criteria"] = [
            "Use official local government planning records.",
            "Record indicators, agencies, caveats, and unknowns.",
        ]
        task["max_images"] = 0
        task.pop("expected_evidence", None)


def _sanitize_narrow_visual_optional_candidate(candidate: dict) -> None:
    for angle in candidate.get("angles", []):
        if angle.get("route") != "text_only":
            angle["route"] = "visual_optional"
            angle["expected_visual_targets"] = angle.get("expected_visual_targets") or [
                "official screenshots or chart images named by the user"
            ]
    for task in candidate.get("bounded_tasks", []):
        if task.get("route") != "text_only":
            task["route"] = "visual_optional"
            task["expected_visual_targets"] = task.get("expected_visual_targets") or [
                "official screenshots or chart images named by the user"
            ]
            task["max_images"] = max(1, min(3, int(task.get("max_images") or 1)))


if __name__ == "__main__":
    raise SystemExit(main())
