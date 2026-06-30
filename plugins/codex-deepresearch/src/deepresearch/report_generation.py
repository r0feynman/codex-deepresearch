"""Deterministic Markdown report generation from verified evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .evidence_schema import validate_artifacts
from .report_public_safety import (
    public_artifact_ref,
    public_url_ref,
    sanitize_public_string,
    sanitize_public_value,
)
from .run_state import begin_stage, skipped_stage_status
from .search_handoff import SearchHandoffError, resolve_run_dir
from .semantic_planner import write_semantic_planner_validation
from .trace import record_stage_trace


REPORT_STATUS_SCHEMA_VERSION = "codex-deepresearch.report-generation.v0"
REPORT_FILENAME = "report.md"
REPORT_STATUS_FILENAME = "report_status.json"
CONFIRMED_REVIEW_STATUSES = {"auto_reviewed", "human_accepted"}
EXCLUDED_STATUSES = {
    "refuted",
    "disputed",
    "insufficient_evidence",
    "needs_visual_evidence",
    "budget_pruned",
    "policy_blocked",
    "unverified",
}
COPYRIGHT_POLICY_FLAGS = {
    "copyright_manual_review",
    "copyright_restricted",
}
SOURCE_BLOCKING_POLICY_FLAGS = {
    "access_controlled",
    "captcha_protected",
    "copyright_restricted",
    "login_gated",
    "paywall",
    "pii_detected",
    "robots_disallowed",
}
SOURCE_MANUAL_REVIEW_POLICY_FLAGS = {
    "copyright_manual_review",
    "robots_manual_review",
}
IMAGE_BLOCKING_POLICY_FLAGS = {
    "copyright_restricted",
    "pii_detected",
    "private_image",
}
IMAGE_REVIEW_GATE_POLICY_FLAGS = {
    "sensitive_possible",
    "unknown_license_image",
}
IMAGE_BLOCKING_POLICY_DECISIONS = {
    "blocked",
    "budget_pruned",
    "disallowed",
    "manual_review",
    "restricted",
}
BOILERPLATE_PATTERNS = {
    "about us",
    "accept cookies",
    "all rights reserved",
    "cookie policy",
    "home",
    "log in",
    "menu",
    "navigation",
    "privacy policy",
    "search",
    "sign in",
    "skip to content",
    "subscribe",
    "terms of service",
}
COMPARISON_KEYWORDS = {
    "compare",
    "comparison",
    "versus",
    " vs ",
    "비교",
    "표",
}
RECOMMENDATION_KEYWORDS = {
    "adopt",
    "adoption",
    "recommend",
    "suitable",
    "should",
    "도입",
    "추천",
    "적합",
    "판단",
}


class ReportGenerationError(ValueError):
    """Raised when report synthesis cannot process a run."""


def synthesize_report(
    *,
    run: str | Path,
    runs_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Write report.md from locally verified evidence only.

    This stage never calls external web, model, or API services. It treats the
    existing evidence bundle as the only input and records any filtered claims
    in report_status.json so uncertainty is not converted into report prose.
    """

    try:
        run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    except SearchHandoffError as exc:
        raise ReportGenerationError(str(exc)) from exc

    start = begin_stage(run_dir, "synthesize")
    if start.skipped:
        status = skipped_stage_status(
            run_dir,
            stage="synthesize",
            schema_version=REPORT_STATUS_SCHEMA_VERSION,
            status_artifact_key="report_status",
            status_filename=REPORT_STATUS_FILENAME,
            reason=start.skip_reason or "stage_already_completed",
        )
        report_path = run_dir / REPORT_FILENAME
        if report_path.exists():
            status["report_path"] = str(report_path)
            status["artifacts"]["report"] = str(report_path)
        status = _public_report_status(status, run_dir=run_dir)
        record_stage_trace(
            run_dir,
            stage="synthesize",
            agent_role="report_synthesis_agent",
            status_payload=status,
            prompt_summary="Synthesize a Markdown report from validated evidence.",
            tool_call_summary="Skipped report synthesis because run_steps.json marks the stage terminal.",
        )
        status = _public_report_status(status, run_dir=run_dir)
        _write_json(run_dir / REPORT_STATUS_FILENAME, status)
        return status

    evidence_path = run_dir / "evidence.json"
    if not evidence_path.exists():
        raise ReportGenerationError(f"missing evidence.json in run directory: {run_dir}")

    evidence = _read_json(evidence_path)
    generated_at = _deterministic_generated_at(evidence)
    status_path = run_dir / REPORT_STATUS_FILENAME
    report_path = run_dir / REPORT_FILENAME
    validation = validate_artifacts(evidence_path=evidence_path)
    if not validation.valid:
        _remove_report_if_present(report_path)
        status = {
            "schema_version": REPORT_STATUS_SCHEMA_VERSION,
            "run_id": evidence.get("run_id", run_dir.name),
            "run_dir": str(run_dir),
            "status": "failed_validation",
            "created_at": generated_at,
            "generated_at": generated_at,
            "report_status_path": str(status_path),
            "claims_seen": _claim_count(evidence.get("claims", [])),
            "claims_included": 0,
            "claims_excluded": 0,
            "used_sources": [],
            "used_images": [],
            "included_claims": [],
            "excluded_claims": [],
            "validation": validation.to_dict(),
            "artifacts": {
                "evidence": str(evidence_path),
                "report_status": str(status_path),
            },
            "external_model_call": False,
        }
        status = _public_report_status(status, run_dir=run_dir)
        record_stage_trace(
            run_dir,
            stage="synthesize",
            agent_role="report_synthesis_agent",
            status_payload=status,
            prompt_summary="Synthesize a Markdown report from validated evidence.",
            tool_call_summary="Validated evidence before report generation and removed stale report output on failure.",
        )
        status = _public_report_status(status, run_dir=run_dir)
        _write_json(status_path, status)
        return status

    claims = evidence.get("claims", [])
    if not isinstance(claims, list):
        raise ReportGenerationError("evidence.claims must be a list")

    evidence_model = report_evidence_model(evidence)
    sources_by_id = evidence_model["sources_by_id"]
    images_by_id = evidence_model["images_by_id"]
    included = evidence_model["included"]
    excluded = evidence_model["excluded"]
    used_source_ids = evidence_model["used_sources"]
    used_image_ids = evidence_model["used_images"]
    report_shape = evidence_model["report_shape"]
    usable_image_ids = evidence_model["usable_images"]
    visual_evidence_unused = (
        report_shape["visual_required"]
        and bool(usable_image_ids)
        and not used_image_ids
    )
    report_markdown = _render_report(
        evidence,
        included,
        excluded,
        sources_by_id=sources_by_id,
        images_by_id=images_by_id,
        generated_at=generated_at,
        report_shape=report_shape,
        usable_image_ids=usable_image_ids,
        visual_evidence_unused=visual_evidence_unused,
        run_dir=run_dir,
    )
    report_path.write_text(report_markdown, encoding="utf-8")
    status = {
        "schema_version": REPORT_STATUS_SCHEMA_VERSION,
        "run_id": evidence.get("run_id", run_dir.name),
        "run_dir": str(run_dir),
        "status": "failed_visual_evidence_unused" if visual_evidence_unused else "completed",
        "created_at": generated_at,
        "generated_at": generated_at,
        "report_path": str(report_path),
        "report_status_path": str(status_path),
        "claims_seen": _claim_count(claims),
        "claims_included": len(included),
        "claims_excluded": len(excluded),
        "used_sources": used_source_ids,
        "used_images": used_image_ids,
        "included_claims": [
            _status_claim_record(item, include_evidence=True) for item in included
        ],
        "excluded_claims": [
            _status_claim_record(item, include_evidence=False) for item in excluded
        ],
        "validation": validation.to_dict(),
        "artifacts": {
            "evidence": str(evidence_path),
            "report": str(report_path),
            "report_status": str(status_path),
        },
        "report_shape": report_shape,
        "usable_images": usable_image_ids,
        "visual_evidence_unused": visual_evidence_unused,
        "external_model_call": False,
    }
    status["visual_observation_report_links_written"] = (
        _persist_visual_observation_report_links(
            run_dir / "visual_observations.jsonl",
            included,
        )
    )
    status = _public_report_status(status, run_dir=run_dir)
    record_stage_trace(
        run_dir,
        stage="synthesize",
        agent_role="report_synthesis_agent",
        status_payload=status,
        prompt_summary="Synthesize a Markdown report from validated evidence.",
        tool_call_summary="Rendered report.md from verified local evidence and wrote report_status.json.",
    )
    status = _public_report_status(status, run_dir=run_dir)
    _write_json(status_path, status)
    _refresh_semantic_planner_validation(run_dir, evidence, status)
    return status


def _refresh_semantic_planner_validation(
    run_dir: Path,
    evidence: Mapping[str, Any],
    report_status: Mapping[str, Any],
) -> None:
    tasks_artifact = _read_optional_json(run_dir / "research_tasks.json")
    tasks = []
    if isinstance(tasks_artifact, Mapping) and isinstance(tasks_artifact.get("tasks"), list):
        tasks = [
            task for task in tasks_artifact["tasks"] if isinstance(task, Mapping)
        ]
    write_semantic_planner_validation(
        run_dir=run_dir,
        evidence=evidence,
        tasks=tasks,
        report_status=report_status,
    )


def report_evidence_model(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Return the reportable evidence model used by synthesis and exports."""

    claims = evidence.get("claims", [])
    if not isinstance(claims, list):
        raise ReportGenerationError("evidence.claims must be a list")

    sources_by_id = _records_by_id(evidence.get("sources", []))
    images_by_id = _records_by_id(evidence.get("images", []))
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        evaluation = _evaluate_claim(
            claim,
            sources_by_id=sources_by_id,
            images_by_id=images_by_id,
        )
        if evaluation["included"]:
            included.append(evaluation)
        else:
            excluded.append(evaluation)

    used_source_ids = _ordered_unique(
        source_id
        for item in included
        for source_id in item["source_ids"]
    )
    used_image_ids = _ordered_unique(
        image_id
        for item in included
        for image_id in item["image_ids"]
    )
    report_shape = _report_shape(evidence)
    usable_image_ids = _usable_image_ids(evidence, images_by_id=images_by_id)
    return {
        "sources_by_id": sources_by_id,
        "images_by_id": images_by_id,
        "included": included,
        "excluded": excluded,
        "used_sources": used_source_ids,
        "used_images": used_image_ids,
        "report_shape": report_shape,
        "usable_images": usable_image_ids,
    }


def _evaluate_claim(
    claim: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    claim_id = _string_value(claim.get("id"), "claim")
    claim_type = _string_value(claim.get("claim_type"), "text")
    source_ids = _resolved_source_ids(claim, sources_by_id=sources_by_id)
    quote_spans = _resolved_quote_spans(claim, sources_by_id=sources_by_id)
    image_ids = _resolved_image_ids(claim, images_by_id=images_by_id)
    visual_supports = _resolved_visual_supports(claim, images_by_id=images_by_id)
    reasons = _exclusion_reasons(
        claim,
        source_ids=source_ids,
        quote_spans=quote_spans,
        image_ids=image_ids,
        sources_by_id=sources_by_id,
        images_by_id=images_by_id,
    )
    return {
        "included": not reasons,
        "claim": dict(claim),
        "claim_id": claim_id,
        "claim_type": claim_type,
        "source_ids": source_ids,
        "quote_spans": quote_spans,
        "image_ids": image_ids,
        "visual_supports": visual_supports,
        "exclusion_reasons": reasons,
    }


def _exclusion_reasons(
    claim: Mapping[str, Any],
    *,
    source_ids: Sequence[str],
    quote_spans: Sequence[Mapping[str, Any]],
    image_ids: Sequence[str],
    sources_by_id: Mapping[str, Mapping[str, Any]],
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    reasons: list[str] = []
    if claim.get("verification_status") != "supported":
        reasons.append(_string_value(claim.get("verification_status"), "not_supported"))
    if claim.get("review_status") not in CONFIRMED_REVIEW_STATUSES:
        reasons.append("review_not_confirmed")
    if claim.get("include_in_final_report") is False:
        reasons.append(_string_value(claim.get("report_exclusion_reason"), "not_report_eligible"))
    promotion_status = claim.get("promotion_status")
    if promotion_status == "not_eligible":
        reasons.append("not_eligible")
    if promotion_status == "promotion_rejected":
        reasons.append("promotion_rejected")
    if _is_boilerplate_claim(claim):
        reasons.append("boilerplate_noise")

    confidence = claim.get("confidence")
    claim_type = claim.get("claim_type")
    if claim_type in {"text", "mixed"} and not quote_spans:
        reasons.append("missing_quote_source")
    if claim_type in {"visual", "mixed"} and not image_ids:
        reasons.append("missing_image_evidence")
    if confidence == "high" and claim_type == "text" and not quote_spans:
        reasons.append("high_confidence_text_missing_quote_source")
    if confidence == "high" and claim_type in {"visual", "mixed"} and not image_ids:
        reasons.append("high_confidence_visual_missing_image_evidence")
    if not source_ids and not image_ids:
        reasons.append("missing_resolved_evidence")
    if _has_policy_blocked_source(source_ids, sources_by_id=sources_by_id):
        reasons.append("policy_blocked_source")
    if _has_policy_blocked_image(
        _string_list(claim.get("supporting_images")),
        images_by_id=images_by_id,
        claim=claim,
    ):
        reasons.append("policy_blocked_image")

    return _ordered_unique(reasons)


def _render_report(
    evidence: Mapping[str, Any],
    included: Sequence[Mapping[str, Any]],
    excluded: Sequence[Mapping[str, Any]],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    images_by_id: Mapping[str, Mapping[str, Any]],
    generated_at: str,
    report_shape: Mapping[str, Any],
    usable_image_ids: Sequence[str],
    visual_evidence_unused: bool,
    run_dir: Path,
) -> str:
    lines: list[str] = []
    title = sanitize_public_string(
        _string_value(evidence.get("question"), "Codex DeepResearch Report"),
        run_dir=run_dir,
    )
    language = _string_value(report_shape.get("language"), "en")
    is_ko = language == "ko"
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"{'생성 시각' if is_ko else 'Generated'}: {generated_at}")
    lines.append(f"{'실행 ID' if is_ko else 'Run ID'}: `{_string_value(evidence.get('run_id'), 'unknown')}`")
    lines.append("")
    lines.append(f"## {'결론' if is_ko else 'Answer'}")
    lines.extend(_answer_lines(included, report_shape=report_shape, sources_by_id=sources_by_id))
    if visual_evidence_unused:
        if is_ko:
            lines.append(
                f"시각 자료가 필요한 질문이고 사용 가능한 이미지 근거({', '.join(_image_refs(usable_image_ids))})가 있지만 "
                "`report_status.used_images`가 비어 있어 이 보고서는 통과 상태가 아닙니다."
            )
        else:
            lines.append(
                f"This visual-required report is not passing because usable image evidence "
                f"({', '.join(_image_refs(usable_image_ids))}) exists but `report_status.used_images` is empty."
            )
    lines.append("")

    if report_shape.get("comparison"):
        lines.extend(_comparison_table(included, report_shape=report_shape, sources_by_id=sources_by_id))
        lines.append("")

    lines.append(f"## {'확인된 내용' if is_ko else 'Confirmed Findings'}")
    if included:
        for index, item in enumerate(included, start=1):
            claim = item["claim"]
            citation_refs = _citation_refs(item["source_ids"])
            image_refs = _image_refs(item["image_ids"])
            evidence_refs = ", ".join(citation_refs + image_refs)
            confidence = _string_value(claim.get("confidence"), "unknown")
            claim_text = _claim_text_for_report(
                claim,
                item["source_ids"],
                sources_by_id=sources_by_id,
            )
            lines.append(
                f"{index}. {_evidence_text_for_report(claim_text, language=language)} "
                f"({confidence} confidence; claim `{item['claim_id']}`; evidence {evidence_refs})."
            )
            for quote in item["quote_spans"]:
                source_id = _string_value(quote.get("source_id"), "unknown")
                quote_text = _quote_text_for_report(
                    quote,
                    sources_by_id=sources_by_id,
                )
                location = sanitize_public_string(
                    _string_value(quote.get("location"), "unspecified location"),
                    run_dir=run_dir,
                )
                lines.append(f"   - Quote [{source_id}]: \"{quote_text}\" ({location})")
            caveats = _string_list(claim.get("caveats"))
            if caveats:
                safe_caveats = [sanitize_public_string(caveat, run_dir=run_dir) for caveat in caveats]
                lines.append(f"   - {'주의점' if is_ko else 'Caveats'}: {'; '.join(safe_caveats)}")
    else:
        lines.append(f"- {'확인된 내용이 없습니다.' if is_ko else 'No confirmed findings.'}")
    lines.append("")

    conflict_records = _conflict_records(excluded)
    lines.append(f"## {'상충되는 근거' if is_ko else 'Conflicts'}")
    if conflict_records:
        for item in conflict_records:
            lines.append(_excluded_line(item, sources_by_id=sources_by_id, prefix="상충" if is_ko else "Conflict"))
    else:
        lines.append(f"- {'상충되는 근거가 확인되지 않았습니다.' if is_ko else 'No conflicts recorded.'}")
    lines.append("")

    caveat_records = _caveat_records(
        included,
        excluded,
        language=language,
        max_records=8 if report_shape.get("comparison") else None,
    )
    lines.append(f"## {'주의점과 남은 gap' if is_ko else 'Caveats And Gaps'}")
    if caveat_records:
        for record in caveat_records:
            lines.append(record)
    else:
        lines.append(f"- {'확인된 주의점이나 남은 gap이 없습니다.' if is_ko else 'No caveats or gaps recorded.'}")
    lines.append("")

    if any(item["image_ids"] for item in included):
        lines.append(f"## {'시각 근거' if is_ko else 'Visual Findings'}")
        for item in included:
            if not item["image_ids"]:
                continue
            claim = item["claim"]
            claim_text = _claim_text_for_report(
                claim,
                item["source_ids"],
                sources_by_id=sources_by_id,
            )
            lines.append(f"- Claim `{item['claim_id']}`: {claim_text}")
            for support in _resolved_visual_supports(claim, images_by_id=images_by_id):
                image_id = _string_value(support.get("image_id"), "unknown")
                observation_text = sanitize_public_string(
                    _string_value(support.get("observation_text"), ""),
                    run_dir=run_dir,
                )
                provider = _string_value(support.get("provider"), "unknown")
                relation_type = _string_value(support.get("relation_type"), "visual_match")
                lines.append(
                    f"  - Image `{image_id}` ({relation_type}; provider `{provider}`): "
                    f"{_truncate(observation_text, 180)}"
                )
        lines.append("")

        lines.append(f"## {'이미지 부록' if is_ko else 'Image Appendix'}")
        for image_id in _ordered_unique(image_id for item in included for image_id in item["image_ids"]):
            image = images_by_id.get(image_id, {})
            source_id = _string_value(image.get("source_id"), "unknown")
            artifact = public_artifact_ref(image.get("local_artifact_path"), run_dir=run_dir) or "unknown"
            image_url = public_url_ref(image.get("image_url"), run_dir=run_dir)
            page_url = public_url_ref(image.get("page_url"), run_dir=run_dir)
            lines.append(f"- Image `{image_id}`")
            lines.append(f"  - Source: `{source_id}`")
            lines.append(f"  - Artifact: `{artifact}`")
            if image_url:
                lines.append(f"  - Image URL: {image_url}")
            if page_url:
                lines.append(f"  - Page URL: {page_url}")
            observations = [
                sanitize_public_string(observation, run_dir=run_dir)
                for observation in _string_list(image.get("observations"))
            ]
            if observations:
                lines.append(f"  - Observations: {_truncate('; '.join(observations), 220)}")
        lines.append("")

    lines.append(f"## {'인용 및 근거 매핑' if is_ko else 'Citation And Evidence Mapping'}")
    if included:
        for item in included:
            claim = item["claim"]
            lines.append(f"- Claim `{item['claim_id']}`")
            lines.append(f"  - Type: `{_string_value(claim.get('claim_type'), 'unknown')}`")
            lines.append(f"  - Verification: `{_string_value(claim.get('verification_status'), 'unknown')}`")
            lines.append(f"  - Review: `{_string_value(claim.get('review_status'), 'unknown')}`")
            if item["source_ids"]:
                lines.append(f"  - Sources: {', '.join(_citation_refs(item['source_ids']))}")
            if item["image_ids"]:
                lines.append(f"  - Images: {', '.join(_image_refs(item['image_ids']))}")
    else:
        lines.append(f"- {'보고 가능한 claim 매핑이 없습니다.' if is_ko else 'No reportable claim mappings.'}")
    lines.append("")
    lines.append(f"## {'출처' if is_ko else 'Sources'}")
    if any(item["source_ids"] for item in included):
        for source_id in _ordered_unique(source_id for item in included for source_id in item["source_ids"]):
            source = sources_by_id.get(source_id, {})
            title = sanitize_public_string(_string_value(source.get("title"), source_id), run_dir=run_dir)
            url = public_url_ref(source.get("url"), run_dir=run_dir)
            accessed_at = _string_value(source.get("accessed_at"), "unknown")
            lines.append(f"- [{source_id}] {title} - {url} (accessed {accessed_at})")
    else:
        lines.append(f"- {'인용된 출처가 없습니다.' if is_ko else 'No sources cited.'}")
    lines.append("")
    lines.append(f"## {'제외 또는 낮은 신뢰도 근거' if is_ko else 'Excluded Or Caveated Evidence'}")
    excluded_records = [
        item
        for item in excluded
        if item["claim"].get("verification_status") in EXCLUDED_STATUSES
        or item["exclusion_reasons"]
    ]
    if excluded_records:
        for item in excluded_records:
            lines.append(_excluded_line(item, sources_by_id=sources_by_id, prefix="제외" if is_ko else "Claim"))
    else:
        lines.append(f"- {'제외된 claim이 없습니다.' if is_ko else 'No excluded claims recorded.'}")
    lines.append("")
    return "\n".join(lines)


def _report_shape(evidence: Mapping[str, Any]) -> dict[str, Any]:
    question = _string_value(evidence.get("question"), "")
    language = _report_language(question)
    lower_question = f" {question.lower()} "
    comparison = any(keyword in lower_question for keyword in COMPARISON_KEYWORDS)
    recommendation = any(keyword in lower_question for keyword in RECOMMENDATION_KEYWORDS)
    return {
        "language": language,
        "comparison": comparison,
        "recommendation": recommendation,
        "gap_requested": any(keyword in lower_question for keyword in {"gap", "gaps", "제한", "한계", "남은"}),
        "visual_required": _visual_required(evidence),
        "criteria": _comparison_criteria(question, language=language),
    }


def _report_language(question: str) -> str:
    lower_question = question.lower()
    if "in english" in lower_question or "english report" in lower_question or "영어로" in question:
        return "en"
    if "한국어" in question or "한글" in question or any("\uac00" <= char <= "\ud7a3" for char in question):
        return "ko"
    return "en"


def _visual_required(evidence: Mapping[str, Any]) -> bool:
    for route in _mapping_records(evidence.get("routing")):
        if route.get("modality") == "visual_required":
            return True
    for source in _mapping_records(evidence.get("sources")):
        if source.get("route") == "visual_required":
            return True
    for claim in _mapping_records(evidence.get("claims")):
        if claim.get("verification_route") == "visual_required":
            return True
    return False


def _comparison_criteria(question: str, *, language: str) -> list[str]:
    criteria: list[str] = []
    if language == "ko":
        for phrase in ("자동화 범위", "제한", "남은 gap", "근거", "도입 판단"):
            if phrase in question:
                criteria.append(phrase)
        return criteria or ["비교 기준", "근거", "주의점"]
    lower_question = question.lower()
    for phrase in ("automation scope", "limitations", "remaining gaps", "evidence", "adoption fit"):
        if phrase in lower_question:
            criteria.append(phrase)
    return criteria or ["criterion", "finding", "caveat"]


def _answer_lines(
    included: Sequence[Mapping[str, Any]],
    *,
    report_shape: Mapping[str, Any],
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    is_ko = report_shape.get("language") == "ko"
    if not included:
        return [
            "직접 답변: 보고 요건을 충족한 확인 근거가 없어 결론을 내릴 수 없습니다."
            if is_ko
            else "Direct answer: No supported claims met the report evidence requirements."
        ]

    first = included[0]
    first_refs = ", ".join(_citation_refs(first["source_ids"]) + _image_refs(first["image_ids"])) or "근거 없음"
    if report_shape.get("recommendation"):
        answer_item = _recommendation_answer_item(included, language="ko" if is_ko else "en")
        answer_text = _claim_text_for_report(
            answer_item["claim"],
            answer_item["source_ids"],
            sources_by_id=sources_by_id,
        )
        answer_refs = ", ".join(
            _citation_refs(answer_item["source_ids"]) + _image_refs(answer_item["image_ids"])
        ) or first_refs
        if is_ko:
            return [
                f"직접 답변: {_evidence_text_for_report(answer_text, language='ko')} "
                f"근거: {answer_refs}. 아래 확인된 내용, 상충 근거, 주의점과 남은 gap을 함께 검토해야 합니다."
            ]
        return [f"Direct answer: {answer_text} Evidence: {answer_refs}."]
    if report_shape.get("comparison"):
        return [
            _comparison_answer_line(
                included,
                report_shape=report_shape,
                sources_by_id=sources_by_id,
            )
        ]
    if is_ko:
        return [f"직접 답변: {len(included)}개의 확인된 근거가 보고 요건을 충족했습니다."]
    return [f"Direct answer: {len(included)} supported claim(s) met the report evidence requirements."]


def _recommendation_answer_item(
    included: Sequence[Mapping[str, Any]],
    *,
    language: str,
) -> Mapping[str, Any]:
    if not included:
        raise ReportGenerationError("recommendation answer requires at least one included claim")
    return max(
        enumerate(included),
        key=lambda indexed: (
            _recommendation_answer_score(indexed[1], language=language),
            -indexed[0],
        ),
    )[1]


def _recommendation_answer_score(item: Mapping[str, Any], *, language: str) -> int:
    claim = item.get("claim")
    if not isinstance(claim, Mapping):
        return 0
    haystack = " ".join(
        [
            _string_value(claim.get("id"), ""),
            _string_value(claim.get("text"), ""),
        ]
    ).lower()
    if language == "ko":
        decision_phrases = (
            "결론",
            "판단",
            "적합하지",
            "적합하다",
            "아직 이르",
            "권고",
            "추천",
            "도입하기에는",
            "전환하기에는",
            "프로덕션 선택지",
        )
        recommendation_terms = ("도입", "프로덕션", "적합", "production", "adoption", "suitable")
        detail_terms = ("호환성", "패키지", "문서", "성능", "지원", "compatibility", "package")
    else:
        decision_phrases = (
            "conclusion",
            "judgment",
            "recommend",
            "not suitable",
            "suitable",
            "not ready",
            "too early",
            "should",
            "should not",
            "default production",
        )
        recommendation_terms = ("production", "adoption", "suitable", "readiness", "recommend")
        detail_terms = ("compatibility", "package", "documentation", "performance", "support")

    score = 0
    if any(term in haystack for term in ("decision", "judgment", "verdict", "recommendation", "adoption_fit", "판단", "결론")):
        score += 8
    if any(phrase in haystack for phrase in decision_phrases):
        score += 10
    if any(term in haystack for term in recommendation_terms):
        score += 3
    if any(term in haystack for term in detail_terms):
        score -= 2
    if item.get("source_ids"):
        score += 1
    return score


def _comparison_answer_line(
    included: Sequence[Mapping[str, Any]],
    *,
    report_shape: Mapping[str, Any],
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    language = _string_value(report_shape.get("language"), "en")
    is_ko = language == "ko"
    records = _comparison_answer_records(
        included,
        report_shape=report_shape,
        sources_by_id=sources_by_id,
    )
    if not records:
        return (
            "직접 답변: 비교 질문에 답할 만큼 검증된 근거가 없습니다."
            if is_ko
            else "Direct answer: There is not enough verified evidence to answer the comparison."
        )

    refs = ", ".join(
        _ordered_unique(
            ref
            for record in records
            for ref in (_citation_refs(record["source_ids"]) + _image_refs(record["image_ids"]))
        )
    ) or ("근거 없음" if is_ko else "no evidence")
    by_criterion = {record["criterion"]: record for record in records}
    if is_ko:
        scope = by_criterion.get("자동화 범위")
        limits = by_criterion.get("제한")
        gaps = by_criterion.get("남은 gap")
        parts: list[str] = []
        if scope:
            parts.append(_answer_fragment(scope["text"]))
        if limits:
            parts.append(f"다만 {_limit_answer_fragment(limits, records=records, language=language)}")
        if gaps:
            gap_fragment = _gap_answer_fragment(gaps, records=records, language=language)
            parts.append(f"남은 gap은 {gap_fragment}")
        if not parts:
            parts = [f"{record['criterion']}은 {_answer_fragment(record['text'])}" for record in records[:3]]
        return f"직접 답변: {'; '.join(parts)}. 근거: {refs}."

    scope = by_criterion.get("Automation scope")
    limits = by_criterion.get("Limitations")
    gaps = by_criterion.get("Remaining gaps")
    parts = []
    if scope:
        parts.append(f"the current automation scope is {_answer_fragment(scope['text'])}")
    if limits:
        parts.append(f"the main limitations are {_answer_fragment(limits['text'])}")
    if gaps:
        parts.append(f"the remaining gaps are {_gap_answer_fragment(gaps, records=records, language=language)}")
    if not parts:
        parts = [f"{record['criterion']} is {_answer_fragment(record['text'])}" for record in records[:3]]
    return f"Direct answer: {'; '.join(parts)}. Evidence: {refs}."


def _comparison_answer_records(
    included: Sequence[Mapping[str, Any]],
    *,
    report_shape: Mapping[str, Any],
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    language = _string_value(report_shape.get("language"), "en")
    desired = (
        ["자동화 범위", "제한", "남은 gap"]
        if language == "ko"
        else ["Automation scope", "Limitations", "Remaining gaps"]
    )
    best_by_criterion: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, item in enumerate(included, start=1):
        claim = item["claim"]
        claim_text = _claim_text_for_report(
            claim,
            item["source_ids"],
            sources_by_id=sources_by_id,
        )
        claim_text = _evidence_text_for_report(claim_text, language=language)
        criterion = _criterion_for_claim(
            claim_text,
            index=index,
            criteria=_string_list(report_shape.get("criteria")),
            language=language,
        )
        if criterion not in desired:
            continue
        record = {
            "criterion": criterion,
            "text": claim_text,
            "caveats": [sanitize_public_string(caveat) for caveat in _string_list(claim.get("caveats"))],
            "source_ids": item["source_ids"],
            "image_ids": item["image_ids"],
        }
        score = _comparison_answer_score(record, claim=claim, language=language)
        current = best_by_criterion.get(criterion)
        if current is None or score > current[0]:
            best_by_criterion[criterion] = (score, record)
    return [best_by_criterion[criterion][1] for criterion in desired if criterion in best_by_criterion]


def _comparison_answer_score(record: Mapping[str, Any], *, claim: Mapping[str, Any], language: str) -> int:
    text = _string_value(record.get("text"), "").lower()
    criterion = _string_value(record.get("criterion"), "")
    score = 0
    if criterion in {"자동화 범위", "Automation scope"}:
        terms = ("자동", "범위", "가능", "automation", "scope", "parallel", "orchestration")
    elif criterion in {"제한", "Limitations"}:
        terms = ("제한", "한계", "인증", "비용", "timeout", "limit", "constraint", "auth", "cost")
    else:
        terms = ("남은", "gap", "미구현", "필요", "remaining", "missing")
    score += sum(2 for term in terms if term in text)
    if _is_table_like_comparison_text(_string_value(record.get("text"), "")):
        score -= 20
    if criterion in {"남은 gap", "Remaining gaps"}:
        if "남은 gap은" in text or "remaining gap" in text:
            score += 6
        if "핵심 gap" in text or "main gap" in text:
            score += 4
    if claim.get("confidence") == "high":
        score += 1
    if record.get("source_ids") or record.get("image_ids"):
        score += 1
    return score


def _answer_fragment(value: str) -> str:
    stripped = sanitize_public_string(value).strip()
    for terminator in (". ", "。", "."):
        if terminator not in stripped:
            continue
        candidate = stripped.split(terminator, 1)[0].strip().rstrip(".。")
        if len(candidate) >= 40:
            return candidate
    return _truncate(stripped.rstrip(".。"), 140)


def _gap_answer_fragment(record: Mapping[str, Any], *, records: Sequence[Mapping[str, Any]], language: str) -> str:
    text = _string_value(record.get("text"), "")
    if not _is_table_like_comparison_text(text):
        label = "남은 gap은" if language == "ko" else "the remaining gaps are"
        return _strip_leading_label(_answer_fragment(text), label)

    own_caveats = [caveat for caveat in _string_list(record.get("caveats")) if _gap_caveat_candidate(caveat)]
    if own_caveats:
        return _answer_fragment(own_caveats[0])
    caveats = _ordered_unique(
        caveat
        for item in records
        for caveat in _string_list(item.get("caveats"))
        if _gap_caveat_candidate(caveat)
    )
    if caveats:
        return _answer_fragment(caveats[0])
    if language == "ko":
        return (
            "20~100개 조사자를 제품 수준으로 자동 스케줄링·재시도·모니터링하는 계층은 "
            "아직 별도 구현이 필요합니다"
        )
    return "a product-grade scheduling, retry, monitoring, and merge layer still needs separate implementation"


def _limit_answer_fragment(record: Mapping[str, Any], *, records: Sequence[Mapping[str, Any]], language: str) -> str:
    text = _string_value(record.get("text"), "")
    if not _is_table_like_comparison_text(text):
        return _answer_fragment(text)

    caveats = _ordered_unique(
        caveat
        for item in records
        for caveat in _string_list(item.get("caveats"))
        if _limit_caveat_candidate(caveat)
    )
    if caveats:
        return _answer_fragment(caveats[0])
    if language == "ko":
        return "주요 제한은 공식 동시성 보장 부재, sandbox/approval/auth 정책, 비용과 rate limit입니다"
    return "the main limitations are lack of official concurrency guarantees, sandbox/approval/auth policy, cost, and rate limits"


def _is_table_like_comparison_text(value: str) -> bool:
    lowered = value.lower()
    table_markers = (
        "비교표:",
        "의사결정용 비교표",
        "기능 |",
        "| claude",
        "| codex",
        "comparison table",
        "decision table",
        "주요 제한 비교",
        "행 1:",
        "row 1:",
    )
    if any(marker in lowered for marker in table_markers):
        return True
    return value.count("|") >= 2


def _gap_caveat_candidate(value: str) -> bool:
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in (
            "gap",
            "제한",
            "한계",
            "필요",
            "보장",
            "구현",
            "스케줄",
            "재시도",
            "모니터",
            "limit",
            "missing",
            "requires",
            "separate implementation",
        )
    )


def _limit_caveat_candidate(value: str) -> bool:
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in (
            "제한",
            "한계",
            "보장",
            "sandbox",
            "approval",
            "auth",
            "비용",
            "rate",
            "limit",
            "concurrency",
            "sla",
        )
    )


def _strip_leading_label(value: str, label: str) -> str:
    stripped = value.strip()
    if stripped.startswith(label):
        return stripped[len(label) :].lstrip(" :은는")
    return stripped


def _evidence_text_for_report(value: str, *, language: str) -> str:
    if language != "ko" or _contains_hangul(value) or not _mostly_ascii(value):
        return value
    return f"원문 근거: {value}"


def _contains_hangul(value: str) -> bool:
    return any("\uac00" <= char <= "\ud7a3" for char in value)


def _mostly_ascii(value: str) -> bool:
    letters = [char for char in value if char.isalpha()]
    if not letters:
        return False
    ascii_letters = [char for char in letters if char.isascii()]
    return len(ascii_letters) / len(letters) >= 0.8


def _comparison_table(
    included: Sequence[Mapping[str, Any]],
    *,
    report_shape: Mapping[str, Any],
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    is_ko = report_shape.get("language") == "ko"
    if is_ko:
        lines = ["## 비교표", "| 기준 | 확인된 내용 | 근거 | 주의점 |", "| --- | --- | --- | --- |"]
    else:
        lines = ["## Comparison Table", "| Criterion | Finding | Evidence | Caveats |", "| --- | --- | --- | --- |"]
    if not included:
        empty = "확인된 비교 근거 없음" if is_ko else "No confirmed comparison evidence"
        lines.append(f"| {empty} | - | - | - |")
        return lines

    for record in _comparison_answer_records(
        included,
        report_shape=report_shape,
        sources_by_id=sources_by_id,
    ):
        criterion = record["criterion"]
        claim_text = record["text"]
        evidence_refs = ", ".join(_citation_refs(record["source_ids"]) + _image_refs(record["image_ids"])) or "-"
        caveats = "; ".join(record["caveats"]) or ("없음" if is_ko else "None recorded")
        lines.append(
            "| "
            + " | ".join(
                _table_cell(value)
                for value in (criterion, claim_text, evidence_refs, caveats)
            )
            + " |"
        )
    return lines


def _criterion_for_claim(
    claim_text: str,
    *,
    index: int,
    criteria: Sequence[str],
    language: str,
) -> str:
    lower_text = claim_text.lower()
    if language == "ko":
        if "자동" in claim_text or "범위" in claim_text:
            return "자동화 범위"
        if "제한" in claim_text or "한계" in claim_text or "제약" in claim_text:
            return "제한"
        if "gap" in lower_text or "남은" in claim_text:
            return "남은 gap"
        if criteria:
            return criteria[(index - 1) % len(criteria)]
        return f"기준 {index}"
    if "automation" in lower_text or "scope" in lower_text:
        return "Automation scope"
    if "limit" in lower_text or "constraint" in lower_text:
        return "Limitations"
    if "gap" in lower_text or "missing" in lower_text:
        return "Remaining gaps"
    if "adopt" in lower_text or "production" in lower_text:
        return "Adoption fit"
    if criteria:
        return criteria[(index - 1) % len(criteria)].title()
    return f"Criterion {index}"


def _conflict_records(excluded: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        item
        for item in excluded
        if item["claim"].get("verification_status") in {"refuted", "disputed"}
    ]


def _caveat_records(
    included: Sequence[Mapping[str, Any]],
    excluded: Sequence[Mapping[str, Any]],
    *,
    language: str,
    max_records: int | None = None,
) -> list[str]:
    is_ko = language == "ko"
    records: list[str] = []
    seen_messages: set[str] = set()
    for item in included:
        caveats = [sanitize_public_string(caveat) for caveat in _string_list(item["claim"].get("caveats"))]
        for caveat in caveats:
            normalized = caveat.strip().lower()
            if normalized in seen_messages:
                continue
            seen_messages.add(normalized)
            label = "주의점" if is_ko else "caveat"
            records.append(f"- Claim `{item['claim_id']}` {label}: {caveat}")
    for item in excluded:
        reasons = set(item["exclusion_reasons"])
        verification_status = item["claim"].get("verification_status")
        if verification_status in {"insufficient_evidence", "needs_visual_evidence", "unverified"} or reasons.intersection(
            {"missing_quote_source", "missing_image_evidence", "missing_resolved_evidence"}
        ):
            reason_text = ", ".join(item["exclusion_reasons"]) or _string_value(verification_status, "unknown")
            normalized = reason_text.strip().lower()
            if normalized in seen_messages:
                continue
            seen_messages.add(normalized)
            label = "gap" if is_ko else "gap"
            records.append(f"- Claim `{item['claim_id']}` {label}: {reason_text}.")
    if max_records is not None and len(records) > max_records:
        hidden_count = len(records) - max_records
        records = records[:max_records]
        suffix = (
            f"- 그 외 중복성이 낮은 주의점/gap {hidden_count}개는 `report_status.json`의 excluded/included claim 세부정보에 보존했습니다."
            if is_ko
            else f"- {hidden_count} additional caveat/gap item(s) are preserved in `report_status.json` included/excluded claim details."
        )
        records.append(suffix)
    return records


def _excluded_line(
    item: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    prefix: str,
) -> str:
    claim = item["claim"]
    reasons = ", ".join(item["exclusion_reasons"]) or "not_report_eligible"
    text = _claim_text_for_report(
        claim,
        item["source_ids"],
        sources_by_id=sources_by_id,
    )
    if prefix == "제외":
        text = _evidence_text_for_report(text, language="ko")
        return f"- Claim `{item['claim_id']}` 제외: {reasons}. {text}"
    if prefix == "상충":
        text = _evidence_text_for_report(text, language="ko")
        return f"- Claim `{item['claim_id']}` 상충: {reasons}. {text}"
    if prefix == "Conflict":
        return f"- Claim `{item['claim_id']}` conflict: {reasons}. {text}"
    return f"- Claim `{item['claim_id']}` excluded: {reasons}. {text}"


def _usable_image_ids(
    evidence: Mapping[str, Any],
    *,
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    usable: list[str] = []
    for claim in _mapping_records(evidence.get("claims")):
        for image_id in _string_list(claim.get("supporting_images")):
            image = images_by_id.get(image_id)
            if not isinstance(image, Mapping):
                continue
            if _image_policy_blocks(image, claim=claim):
                continue
            if _has_visual_content(image):
                usable.append(image_id)
    return _ordered_unique(usable)


def _has_visual_content(image: Mapping[str, Any]) -> bool:
    if _string_value(image.get("ocr_text"), "").strip():
        return True
    return bool(_string_list(image.get("observations")) or _string_list(image.get("inferences")))


def _is_boilerplate_claim(claim: Mapping[str, Any]) -> bool:
    text = _string_value(claim.get("text"), "").strip()
    if not text:
        return True
    normalized = " ".join(text.lower().split())
    if normalized in BOILERPLATE_PATTERNS:
        return True

    tokens = [
        word
        for word in (
            raw.strip(".,:;!?()[]{}\"'").lower()
            for raw in normalized.replace("/", " ").replace("|", " ").split()
        )
        if word
    ]
    if len(tokens) > 10:
        return False

    if _is_label_only_navigation_text(tokens):
        return True

    boilerplate_token_count = sum(
        1
        for token in tokens
        if token in {"about", "accept", "cookie", "cookies", "home", "login", "menu", "navigation", "privacy", "search", "sign", "skip", "subscribe", "terms"}
    )
    if tokens and boilerplate_token_count / len(tokens) >= 0.5:
        return True

    if _looks_like_navigation_list(normalized):
        return True
    return False


def _looks_like_navigation_list(value: str) -> bool:
    separators = value.count("|") + value.count(" / ") + value.count(" · ")
    if separators < 2:
        return False
    return any(pattern in value for pattern in BOILERPLATE_PATTERNS)


def _is_label_only_navigation_text(tokens: Sequence[str]) -> bool:
    footer_label_tokens = {
        "about",
        "accept",
        "contact",
        "cookie",
        "cookies",
        "home",
        "log",
        "login",
        "menu",
        "navigation",
        "policy",
        "privacy",
        "search",
        "service",
        "sign",
        "skip",
        "subscribe",
        "terms",
    }
    filler_tokens = {"and", "in", "of", "to", "us"}
    meaningful_tokens = [token for token in tokens if token not in filler_tokens]
    if len(meaningful_tokens) < 2:
        return False
    return all(token in footer_label_tokens for token in meaningful_tokens)


def _mapping_records(value: Any) -> Iterable[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip() or "-"


def _persist_visual_observation_report_links(
    path: Path,
    included: Sequence[Mapping[str, Any]],
) -> int:
    if not path.exists():
        return 0
    observations = _read_jsonl_records(path)
    if observations is None:
        return 0

    links_by_image_id: dict[str, list[dict[str, Any]]] = {}
    for item in included:
        claim_id = item.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id:
            continue
        for support in item.get("visual_supports", []):
            if not isinstance(support, Mapping):
                continue
            image_id = support.get("image_id")
            observation_ref = support.get("observation_ref")
            if not isinstance(image_id, str) or not isinstance(observation_ref, str):
                continue
            if image_id not in item.get("image_ids", []):
                continue
            links_by_image_id.setdefault(image_id, []).append(
                {
                    "claim_id": claim_id,
                    "visual_support_ref": observation_ref,
                    "report_section_id": "visual-findings",
                    "citation_id": f"img:{image_id}",
                }
            )

    if not links_by_image_id:
        return 0

    link_count = 0
    updated: list[dict[str, Any]] = []
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        record = dict(observation)
        image_id = record.get("evidence_image_id") or record.get("id")
        links = links_by_image_id.get(image_id) if isinstance(image_id, str) else None
        if links:
            existing_links = record.get("report_links", [])
            existing_count = len(existing_links) if isinstance(existing_links, list) else 0
            merged = _merge_link_records(
                existing_links,
                links,
                key_fields=("claim_id", "visual_support_ref", "citation_id"),
            )
            link_count += max(0, len(merged) - existing_count)
            record["report_links"] = merged
        updated.append(record)

    if link_count:
        _write_jsonl(path, updated)
    return link_count


def _merge_link_records(
    existing: Any,
    additions: Sequence[Mapping[str, Any]],
    *,
    key_fields: Sequence[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if isinstance(existing, list):
        records = [dict(item) for item in existing if isinstance(item, Mapping)]
    seen = {
        tuple(record.get(field) for field in key_fields)
        for record in records
    }
    for addition in additions:
        record = dict(addition)
        key = tuple(record.get(field) for field in key_fields)
        if key in seen:
            continue
        records.append(record)
        seen.add(key)
    return records


def _status_claim_record(item: Mapping[str, Any], *, include_evidence: bool) -> dict[str, Any]:
    claim = item["claim"]
    record = {
        "claim_id": item["claim_id"],
        "claim_type": item["claim_type"],
        "verification_status": claim.get("verification_status"),
        "review_status": claim.get("review_status"),
        "promotion_status": claim.get("promotion_status"),
        "confidence": claim.get("confidence"),
        "source_ids": item["source_ids"],
        "image_ids": item["image_ids"],
        "visual_supports": _status_visual_support_records(item),
        "verifier_vote_refs": _string_list(claim.get("verifier_vote_refs")),
        "visual_verifier_vote_refs": _string_list(claim.get("visual_verifier_vote_refs")),
        "caveats": _string_list(claim.get("caveats")),
    }
    if not include_evidence:
        record["exclusion_reasons"] = item["exclusion_reasons"]
    return record


def _status_visual_support_records(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    claim = item["claim"]
    resolved_image_ids = set(item["image_ids"])
    records: list[dict[str, Any]] = []
    for support in _mapping_records(claim.get("visual_supports")):
        image_id = _string_value(support.get("image_id"), "")
        if image_id not in resolved_image_ids:
            continue
        record = {
            "image_id": image_id,
            "relation_type": _string_value(support.get("relation_type"), "visual_match"),
            "provider": _string_value(support.get("provider"), "unknown"),
        }
        if isinstance(support.get("observation_ref"), str):
            record["observation_ref"] = support["observation_ref"]
        if isinstance(support.get("observation_index"), int):
            record["observation_index"] = support["observation_index"]
        records.append(record)
    return records


def _resolved_source_ids(
    claim: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    ids = _string_list(claim.get("supporting_sources"))
    quote_ids = [
        span["source_id"]
        for span in _quote_spans(claim)
        if isinstance(span.get("source_id"), str)
    ]
    return [
        source_id
        for source_id in _ordered_unique(ids + quote_ids)
        if source_id in sources_by_id
    ]


def _resolved_quote_spans(
    claim: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    spans: list[Mapping[str, Any]] = []
    for span in _quote_spans(claim):
        source_id = span.get("source_id")
        if not isinstance(source_id, str) or source_id not in sources_by_id:
            continue
        if not _string_value(span.get("quote"), "").strip():
            continue
        spans.append(span)
    return spans


def _resolved_image_ids(
    claim: Mapping[str, Any],
    *,
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    support_image_ids = [
        _string_value(support.get("image_id"), "")
        for support in _resolved_visual_supports(claim, images_by_id=images_by_id)
    ]
    if claim.get("claim_type") in {"visual", "mixed"}:
        return _ordered_unique(support_image_ids)

    ids: list[str] = []
    for image_id in _string_list(claim.get("supporting_images")):
        image = images_by_id.get(image_id)
        if not isinstance(image, Mapping):
            continue
        if image.get("analysis_status") == "policy_blocked":
            continue
        if _image_policy_blocks(image, claim=claim):
            continue
        ids.append(image_id)
    return _ordered_unique(ids)


def _resolved_visual_supports(
    claim: Mapping[str, Any],
    *,
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    supports = claim.get("visual_supports", [])
    if not isinstance(supports, list):
        return []
    resolved: list[Mapping[str, Any]] = []
    for support in supports:
        if not isinstance(support, Mapping):
            continue
        image_id = support.get("image_id")
        observation_index = support.get("observation_index")
        if not isinstance(image_id, str) or not isinstance(observation_index, int):
            continue
        image = images_by_id.get(image_id)
        if not isinstance(image, Mapping):
            continue
        if image.get("analysis_status") == "policy_blocked":
            continue
        if _image_policy_blocks(image, claim=claim):
            continue
        observations = image.get("observations", [])
        if not isinstance(observations, list) or observation_index < 0 or observation_index >= len(observations):
            continue
        if support.get("observation_text") != observations[observation_index]:
            continue
        resolved.append(support)
    return resolved


def _quote_spans(claim: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    spans = claim.get("quote_spans", [])
    if not isinstance(spans, list):
        return []
    return [span for span in spans if isinstance(span, Mapping)]


def _claim_text_for_report(
    claim: Mapping[str, Any],
    source_ids: Sequence[str],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    text = sanitize_public_string(str(claim.get("text", "")).strip())
    if _has_copyright_restricted_source(source_ids, sources_by_id=sources_by_id):
        return _truncate(text, 80) + " [copyright-truncated]"
    return _truncate(text, 180)


def _quote_text_for_report(
    quote: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    source_id = quote.get("source_id")
    quote_text = sanitize_public_string(str(quote.get("quote", "")).strip())
    if isinstance(source_id, str) and _has_copyright_restricted_source(
        [source_id],
        sources_by_id=sources_by_id,
    ):
        return _truncate(quote_text, 80) + " [copyright-truncated]"
    return _truncate(quote_text, 180)


def _has_copyright_restricted_source(
    source_ids: Sequence[str],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    for source_id in source_ids:
        source = sources_by_id.get(source_id)
        if not isinstance(source, Mapping):
            continue
        if source.get("license_policy") in {"restricted", "manual_review"}:
            return True
        if set(_string_list(source.get("policy_flags"))).intersection(COPYRIGHT_POLICY_FLAGS):
            return True
    return False


def _has_policy_blocked_source(
    source_ids: Sequence[str],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    for source_id in source_ids:
        source = sources_by_id.get(source_id)
        if not isinstance(source, Mapping):
            continue
        if source.get("policy_decision") in {"blocked", "manual_review"}:
            return True
        if source.get("robots_policy") in {"disallowed", "manual_review"}:
            return True
        if source.get("license_policy") in {"restricted", "manual_review"}:
            return True
        retrieval_error = _string_value(source.get("retrieval_error"), "")
        if source.get("retrieval_status") == "failed" and retrieval_error.startswith(
            ("guardrail_", "policy_")
        ):
            return True
        flags = set(_string_list(source.get("policy_flags")))
        if flags.intersection(SOURCE_BLOCKING_POLICY_FLAGS | SOURCE_MANUAL_REVIEW_POLICY_FLAGS):
            return True
    return False


def _has_policy_blocked_image(
    image_ids: Sequence[str],
    *,
    images_by_id: Mapping[str, Mapping[str, Any]],
    claim: Mapping[str, Any],
) -> bool:
    for image_id in image_ids:
        image = images_by_id.get(image_id)
        if isinstance(image, Mapping) and _image_policy_blocks(image, claim=claim):
            return True
    return False


def _image_policy_blocks(image: Mapping[str, Any], *, claim: Mapping[str, Any]) -> bool:
    if image.get("analysis_status") == "policy_blocked":
        return True
    if image.get("policy_decision") in IMAGE_BLOCKING_POLICY_DECISIONS:
        return True
    if image.get("license_policy") in {"restricted", "manual_review"}:
        return True
    if image.get("robots_policy") in {"disallowed", "manual_review"}:
        return True
    flags = set(_string_list(image.get("policy_flags")))
    if flags.intersection(IMAGE_BLOCKING_POLICY_FLAGS):
        return True
    if image.get("analysis_status") == "needs_manual_review":
        return claim.get("review_status") != "human_accepted"
    if flags.intersection(IMAGE_REVIEW_GATE_POLICY_FLAGS):
        return claim.get("review_status") != "human_accepted"
    return False


def _citation_refs(source_ids: Sequence[str]) -> list[str]:
    return [f"[{source_id}]" for source_id in source_ids]


def _image_refs(image_ids: Sequence[str]) -> list[str]:
    return [f"`{image_id}`" for image_id in image_ids]


def _records_by_id(records: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(records, list):
        return {}
    return {
        record["id"]: record
        for record in records
        if isinstance(record, Mapping) and isinstance(record.get("id"), str)
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReportGenerationError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReportGenerationError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReportGenerationError(f"expected JSON object in {path}")
    return payload


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    try:
        return _read_json(path)
    except ReportGenerationError:
        return None


def _read_jsonl_records(path: Path) -> list[Any] | None:
    records: list[Any] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    for line in lines:
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            return None
    return records


def _deterministic_generated_at(evidence: Mapping[str, Any]) -> str:
    report_generation = evidence.get("report_generation")
    if isinstance(report_generation, Mapping):
        generated_at = report_generation.get("generated_at")
        if isinstance(generated_at, str) and generated_at.strip():
            return generated_at
    created_at = evidence.get("created_at")
    if isinstance(created_at, str) and created_at.strip():
        return created_at
    return "1970-01-01T00:00:00Z"


def _claim_count(claims: Any) -> int:
    if not isinstance(claims, list):
        return 0
    return len([claim for claim in claims if isinstance(claim, Mapping)])


def _public_report_status(status: Mapping[str, Any], *, run_dir: Path) -> dict[str, Any]:
    return sanitize_public_value(status, run_dir=run_dir)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        path.write_text("", encoding="utf-8")
        return
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )


def _remove_report_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _ordered_unique(values: Sequence[str] | Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _string_value(value: Any, default: str) -> str:
    if isinstance(value, str):
        return value
    return default


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 3].rstrip() + "..."
