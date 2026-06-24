"""Deterministic Markdown report generation from verified evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .evidence_schema import validate_artifacts
from .run_state import begin_stage, skipped_stage_status
from .search_handoff import SearchHandoffError, resolve_run_dir
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
        record_stage_trace(
            run_dir,
            stage="synthesize",
            agent_role="report_synthesis_agent",
            status_payload=status,
            prompt_summary="Synthesize a Markdown report from validated evidence.",
            tool_call_summary="Skipped report synthesis because run_steps.json marks the stage terminal.",
        )
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
        record_stage_trace(
            run_dir,
            stage="synthesize",
            agent_role="report_synthesis_agent",
            status_payload=status,
            prompt_summary="Synthesize a Markdown report from validated evidence.",
            tool_call_summary="Validated evidence before report generation and removed stale report output on failure.",
        )
        _write_json(status_path, status)
        return status

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
    record_stage_trace(
        run_dir,
        stage="synthesize",
        agent_role="report_synthesis_agent",
        status_payload=status,
        prompt_summary="Synthesize a Markdown report from validated evidence.",
        tool_call_summary="Rendered report.md from verified local evidence and wrote report_status.json.",
    )
    _write_json(status_path, status)
    return status


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
    if claim.get("promotion_status") == "promotion_rejected":
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
) -> str:
    lines: list[str] = []
    title = _string_value(evidence.get("question"), "Codex DeepResearch Report")
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
                location = _string_value(quote.get("location"), "unspecified location")
                lines.append(f"   - Quote [{source_id}]: \"{quote_text}\" ({location})")
            caveats = _string_list(claim.get("caveats"))
            if caveats:
                lines.append(f"   - {'주의점' if is_ko else 'Caveats'}: {'; '.join(caveats)}")
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

    caveat_records = _caveat_records(included, excluded, language=language)
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
                observation_text = _string_value(support.get("observation_text"), "")
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
            artifact = _string_value(image.get("local_artifact_path"), "unknown")
            image_url = _string_value(image.get("image_url"), "")
            page_url = _string_value(image.get("page_url"), "")
            lines.append(f"- Image `{image_id}`")
            lines.append(f"  - Source: `{source_id}`")
            lines.append(f"  - Artifact: `{artifact}`")
            if image_url:
                lines.append(f"  - Image URL: {image_url}")
            if page_url:
                lines.append(f"  - Page URL: {page_url}")
            observations = _string_list(image.get("observations"))
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
            title = _string_value(source.get("title"), source_id)
            url = _string_value(source.get("url"), "")
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
        if is_ko:
            return [f"직접 답변: 비교 질문에 대해 {len(included)}개의 확인된 근거를 기준별로 정리했습니다."]
        return [f"Direct answer: The comparison below is based on {len(included)} confirmed finding(s)."]
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
    keywords = (
        ("판단", "적합", "도입", "프로덕션", "production", "adoption", "suitable", "readiness", "judgment")
        if language == "ko"
        else ("production", "adoption", "suitable", "readiness", "judgment", "recommend")
    )
    for item in included:
        claim = item.get("claim")
        if not isinstance(claim, Mapping):
            continue
        haystack = " ".join(
            [
                _string_value(claim.get("id"), ""),
                _string_value(claim.get("text"), ""),
            ]
        ).lower()
        if any(keyword.lower() in haystack for keyword in keywords):
            return item
    return included[0]


def _evidence_text_for_report(value: str, *, language: str) -> str:
    if language != "ko" or not _mostly_ascii(value):
        return value
    return f"원문 근거: {value}"


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

    for index, item in enumerate(included, start=1):
        claim = item["claim"]
        claim_text = _claim_text_for_report(
            claim,
            item["source_ids"],
            sources_by_id=sources_by_id,
        )
        claim_text = _evidence_text_for_report(
            claim_text,
            language=_string_value(report_shape.get("language"), "en"),
        )
        criterion = _criterion_for_claim(
            claim_text,
            index=index,
            criteria=_string_list(report_shape.get("criteria")),
            language=_string_value(report_shape.get("language"), "en"),
        )
        evidence_refs = ", ".join(_citation_refs(item["source_ids"]) + _image_refs(item["image_ids"])) or "-"
        caveats = "; ".join(_string_list(claim.get("caveats"))) or ("없음" if is_ko else "None recorded")
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
        if "제한" in claim_text or "한계" in claim_text:
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
) -> list[str]:
    is_ko = language == "ko"
    records: list[str] = []
    for item in included:
        caveats = _string_list(item["claim"].get("caveats"))
        for caveat in caveats:
            label = "주의점" if is_ko else "caveat"
            records.append(f"- Claim `{item['claim_id']}` {label}: {caveat}")
    for item in excluded:
        reasons = set(item["exclusion_reasons"])
        verification_status = item["claim"].get("verification_status")
        if verification_status in {"insufficient_evidence", "needs_visual_evidence", "unverified"} or reasons.intersection(
            {"missing_quote_source", "missing_image_evidence", "missing_resolved_evidence"}
        ):
            label = "gap" if is_ko else "gap"
            records.append(f"- Claim `{item['claim_id']}` {label}: {', '.join(item['exclusion_reasons']) or verification_status}.")
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


def _status_claim_record(item: Mapping[str, Any], *, include_evidence: bool) -> dict[str, Any]:
    claim = item["claim"]
    record = {
        "claim_id": item["claim_id"],
        "claim_type": item["claim_type"],
        "verification_status": claim.get("verification_status"),
        "review_status": claim.get("review_status"),
        "promotion_status": claim.get("promotion_status"),
        "confidence": claim.get("confidence"),
    }
    if include_evidence:
        record["source_ids"] = item["source_ids"]
        record["image_ids"] = item["image_ids"]
    else:
        record["exclusion_reasons"] = item["exclusion_reasons"]
    return record


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
    text = str(claim.get("text", "")).strip()
    if _has_copyright_restricted_source(source_ids, sources_by_id=sources_by_id):
        return _truncate(text, 80) + " [copyright-truncated]"
    return _truncate(text, 180)


def _quote_text_for_report(
    quote: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    source_id = quote.get("source_id")
    quote_text = str(quote.get("quote", "")).strip()
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


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
