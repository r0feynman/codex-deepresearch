"""Normalize visual analysis handoff records into VisualEvidence."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .cache_keys import image_cache_key
from .evidence_schema import VLM_PROVIDERS, validate_artifacts
from .run_state import begin_stage, skipped_stage_status
from .search_handoff import resolve_run_dir
from .trace import record_stage_trace
from .visual_artifacts import (
    IMAGE_FETCH_STATUS_FILENAME,
    VISUAL_PROVIDER_STATUS_FILENAME,
    VISUAL_PROVIDER_STATUS_SCHEMA_VERSION,
    automatic_visual_status_envelope,
    visual_minimums_for_run,
    validate_visual_artifacts,
)


VISION_ADAPTER_SCHEMA_VERSION = "codex-deepresearch.vision-adapter.v0"
MISSING_VISUAL_STAGE = "vision_adapter_missing_visual"
VISION_ADAPTER_STAGE = "vision_adapter"
CODEX_INTERACTIVE_PROVIDER = "codex-interactive"
CODEX_INTERACTIVE_MODEL = "codex-exec-image-worker"
CODEX_INTERACTIVE_RESPONSE_FILENAME = "codex_interactive_visual_observations.jsonl"
CODEX_INTERACTIVE_BINARY_ENV = "CODEX_DEEPRESEARCH_CODEX_BINARY"
CODEX_INTERACTIVE_TIMEOUT_ENV = "CODEX_DEEPRESEARCH_CODEX_INTERACTIVE_TIMEOUT_SECONDS"
CODEX_INTERACTIVE_MAX_IMAGES_ENV = "CODEX_DEEPRESEARCH_CODEX_INTERACTIVE_MAX_IMAGES"
OPENAI_RESPONSES_VISION_PROVIDER = "openai-responses-vision"
OPENAI_RESPONSES_VISION_MODEL = "gpt-4.1-mini"
OPENAI_RESPONSES_VISION_ENDPOINT = "https://api.openai.com/v1/responses"
OPENAI_RESPONSES_VISION_ALLOW_ENV = "CODEX_DEEPRESEARCH_OPENAI_RESPONSES_VISION_ALLOW_REAL"
OPENAI_RESPONSES_VISION_MODE_ENV = "CODEX_DEEPRESEARCH_OPENAI_RESPONSES_VISION_MODE"
OPENAI_RESPONSES_VISION_MODEL_ENV = "CODEX_DEEPRESEARCH_OPENAI_RESPONSES_VISION_MODEL"
OPENAI_RESPONSES_VISION_DETAIL_ENV = "CODEX_DEEPRESEARCH_OPENAI_RESPONSES_VISION_DETAIL"
OPENAI_RESPONSES_VISION_MAX_IMAGES_ENV = "CODEX_DEEPRESEARCH_OPENAI_RESPONSES_VISION_MAX_IMAGES"
OPENAI_RESPONSES_VISION_ESTIMATED_COST_ENV = (
    "CODEX_DEEPRESEARCH_OPENAI_RESPONSES_VISION_ESTIMATED_COST_USD"
)
OPENAI_RESPONSES_VISION_ACTUAL_COST_ENV = (
    "CODEX_DEEPRESEARCH_OPENAI_RESPONSES_VISION_ACTUAL_COST_USD"
)
OPENAI_RESPONSES_VISION_TIMEOUT_ENV = "CODEX_DEEPRESEARCH_OPENAI_RESPONSES_VISION_TIMEOUT_SECONDS"
OPENAI_RESPONSES_VISION_RESPONSE_FILENAME = "openai_responses_vision_observations.jsonl"
OPENAI_RESPONSES_VISION_SUPPORTED_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
}
TRUE_CONFIG_VALUES = {"1", "true", "yes", "on"}
SENSITIVE_INLINE_KEY_PATTERN = (
    r"[A-Za-z0-9_.-]*(?:api[_-]?key|access[_-]?token|authorization|bearer|token|"
    r"secret|credential|credentials|password|key|auth)[A-Za-z0-9_.-]*"
)
SECRET_FIELD_PATTERN = re.compile(
    r"(?i)\b("
    + SENSITIVE_INLINE_KEY_PATTERN
    + r")(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}\]\)]+)"
)
SECRET_JSON_FIELD_PATTERN = re.compile(
    r"(?i)([\"']?(?:"
    + SENSITIVE_INLINE_KEY_PATTERN
    + r")[\"']?\s*:\s*[\"'])([^\"']+)([\"'])"
)
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")
BEARER_VALUE_PATTERN = re.compile(r"(?i)\b(Bearer)\s+([^\s,;}\]\)]+)")
PRIVATE_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?<![\w.-])/(?:home|Users)/[^\s\"'<>:,;}\]\)]+"
)
SENSITIVE_PROVIDER_KEY_PATTERN = re.compile(r"(?i)" + SENSITIVE_INLINE_KEY_PATTERN)
SENSITIVE_URL_QUERY_KEYS = {
    "access-token",
    "access_token",
    "apikey",
    "api-key",
    "api_key",
    "auth",
    "authorization",
    "bearer",
    "client-secret",
    "client_secret",
    "credential",
    "credentials",
    "key",
    "password",
    "refresh-token",
    "refresh_token",
    "secret",
    "session-token",
    "session_token",
    "token",
}


class VisionAdapterError(ValueError):
    """Raised when visual observations cannot be normalized."""


@dataclass(frozen=True)
class OpenAIResponsesVisionConfig:
    """Resolved configuration for the real Responses API vision adapter."""

    api_key: str | None
    endpoint: str
    model: str
    detail: str
    timeout_seconds: float
    allow_real_vlm: bool
    provider_mode: str
    max_images: int | None
    estimated_cost_usd_per_image: float
    actual_cost_usd_per_image: float


@dataclass(frozen=True)
class OpenAIResponsesVisionResult:
    """Provider response shape consumed by the adapter."""

    observations: tuple[str, ...]
    inferences: tuple[str, ...]
    caveats: tuple[str, ...]
    ocr_text: str | None = None
    confidence: float = 0.7
    response_id: str | None = None
    model: str | None = None
    usage: Mapping[str, Any] | None = None
    raw_provider_metadata: Mapping[str, Any] | None = None
    actual_cost_usd: float | None = None


@dataclass(frozen=True)
class CodexInteractiveVisionConfig:
    """Resolved configuration for the Codex CLI image worker."""

    codex_binary: str
    timeout_seconds: float
    provider_mode: str
    max_images: int | None
    model: str
    project_root: Path


class OpenAIResponsesVisionClient(Protocol):
    def analyze_image(
        self,
        *,
        image_input: str,
        mime_type: str,
        prompt: str,
        config: OpenAIResponsesVisionConfig,
        metadata: Mapping[str, Any],
    ) -> OpenAIResponsesVisionResult:
        """Analyze one image input and return separated observations/inferences."""


class CodexInteractiveVisionClient(Protocol):
    def analyze_image(
        self,
        *,
        image_path: Path,
        mime_type: str,
        prompt: str,
        config: CodexInteractiveVisionConfig,
        metadata: Mapping[str, Any],
    ) -> OpenAIResponsesVisionResult:
        """Analyze one local image artifact with a Codex worker."""


@dataclass(frozen=True)
class _AutomatedVisionAnalysis:
    records: tuple[dict[str, Any], ...]
    observations_path: Path
    provider_status: dict[str, Any]
    estimated_cost_usd: float
    actual_cost_usd: float
    model: str
    provider_mode: str
    provider: str
    external_vlm_call: bool


def ingest_vision_observations(
    *,
    run: str | Path,
    provider: str,
    observations: str | Path | None = None,
    runs_dir: str | Path | None = None,
    provider_mode: str | None = None,
    allow_real_vlm: bool | None = None,
    openai_config: Mapping[str, Any] | None = None,
    openai_client: OpenAIResponsesVisionClient | None = None,
    codex_config: Mapping[str, Any] | None = None,
    codex_client: CodexInteractiveVisionClient | None = None,
) -> dict[str, Any]:
    """Normalize provider-specific visual observations into run evidence.

    Explicit observation ingestion is dry. Automated provider branches run only
    when no observations file is supplied and fetched visual artifacts are
    already present. openai-responses-vision may call the Responses API when
    its real-provider gates are satisfied; codex-interactive uses a Codex CLI
    worker with explicit ``--image`` artifacts.
    """

    normalized_provider = _normalize_provider(provider)
    run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    start = begin_stage(run_dir, "ingest_vision")
    if start.skipped:
        status = skipped_stage_status(
            run_dir,
            stage="ingest_vision",
            schema_version=VISION_ADAPTER_SCHEMA_VERSION,
            status_artifact_key="vision_ingest_status",
            status_filename="vision_ingest_status.json",
            reason=start.skip_reason or "stage_already_completed",
        )
        status["provider"] = normalized_provider
        record_stage_trace(
            run_dir,
            stage="ingest_vision",
            agent_role="vision_adapter",
            status_payload=status,
            prompt_summary="Normalize visual handoff records into VisualEvidence.",
            tool_call_summary="Skipped visual ingestion because run_steps.json marks the stage terminal.",
        )
        _write_json(run_dir / "vision_ingest_status.json", status)
        return status
    evidence_path = run_dir / "evidence.json"
    if not evidence_path.exists():
        raise VisionAdapterError(f"missing evidence.json in run directory: {run_dir}")

    evidence = _read_json(evidence_path)
    records, input_path = _read_observation_records(run_dir, observations)
    now = _utc_now()
    automated_analysis: _AutomatedVisionAnalysis | None = None

    evidence["vlm_provider"] = normalized_provider
    evidence.setdefault("images", [])
    evidence.setdefault("claims", [])
    if not isinstance(evidence["images"], list):
        raise VisionAdapterError("evidence.images must be a list")
    if not isinstance(evidence["claims"], list):
        raise VisionAdapterError("evidence.claims must be a list")

    visual_observations_path = run_dir / "visual_observations.jsonl"
    if (
        normalized_provider == OPENAI_RESPONSES_VISION_PROVIDER
        and observations is None
        and _has_openai_vision_handoff(run_dir)
    ):
        automated_result = _run_openai_responses_vision_analysis(
            run_dir=run_dir,
            evidence=evidence,
            now=now,
            provider_mode=provider_mode,
            allow_real_vlm=allow_real_vlm,
            openai_config=openai_config,
            openai_client=openai_client,
        )
        if isinstance(automated_result, dict):
            return automated_result
        automated_analysis = automated_result
        records = list(automated_analysis.records)
        input_path = automated_analysis.observations_path
    elif (
        normalized_provider == CODEX_INTERACTIVE_PROVIDER
        and observations is None
        and not records
        and _has_openai_vision_handoff(run_dir)
    ):
        automated_result = _run_codex_interactive_vision_analysis(
            run_dir=run_dir,
            evidence=evidence,
            now=now,
            provider_mode=provider_mode,
            codex_config=codex_config,
            codex_client=codex_client,
        )
        if isinstance(automated_result, dict):
            return automated_result
        automated_analysis = automated_result
        records = list(automated_analysis.records)
        input_path = automated_analysis.observations_path

    errors: list[dict[str, Any]] = []
    normalized_images: list[dict[str, Any]] = []
    fetch_lineage_records_reconciled = 0
    existing_image_ids = _existing_ids(evidence["images"])
    used_image_ids: set[str] = set()
    sources_by_id = _sources_by_id(evidence.get("sources", []))
    source_ids = set(sources_by_id)

    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            errors.append({"code": "invalid_record", "record_index": index})
            continue
        try:
            normalized_images.append(
                _normalize_visual_record(
                    record,
                    provider=normalized_provider,
                    index=index,
                    run_dir=run_dir,
                    sources_by_id=sources_by_id,
                    existing_image_ids=existing_image_ids,
                    used_image_ids=used_image_ids,
                    now=now,
                )
            )
        except VisionAdapterError as exc:
            errors.append(
                {
                    "code": "normalization_failed",
                    "record_index": index,
                    "detail": str(exc),
                }
            )

    if errors:
        status = _base_status(run_dir, evidence, "failed_normalization")
        status["errors"] = errors
        status["artifacts"] = {
            "evidence": str(evidence_path),
            "visual_observations": str(run_dir / "visual_observations.jsonl"),
            "vision_ingest_status": str(run_dir / "vision_ingest_status.json"),
        }
        record_stage_trace(
            run_dir,
            stage="ingest_vision",
            agent_role="vision_adapter",
            status_payload=status,
            prompt_summary="Normalize visual handoff records into VisualEvidence.",
            tool_call_summary="Read visual observation records and validated run-local image/source references.",
        )
        _write_json(run_dir / "vision_ingest_status.json", status)
        return status

    evidence["claims"] = [
        claim
        for claim in evidence["claims"]
        if not (
            isinstance(claim, Mapping)
            and claim.get("extraction_stage") == MISSING_VISUAL_STAGE
        )
    ]

    images_reused = 0
    if normalized_images:
        phase3_observations = _phase3_observation_records(
            records,
            normalized_images,
            provider=normalized_provider,
            now=now,
        )
        normalized_images, images_reused = _reuse_completed_images(
            evidence["images"],
            normalized_images,
        )
        new_ids = {image["id"] for image in normalized_images}
        evidence["images"] = [
            image
            for image in evidence["images"]
            if not (
                isinstance(image, Mapping)
                and (
                    image.get("id") in new_ids
                    or (
                        image.get("vision_adapter_stage") == VISION_ADAPTER_STAGE
                        and image.get("analysis_provider") == normalized_provider
                    )
                )
            )
        ] + normalized_images
        _write_jsonl(visual_observations_path, phase3_observations)
        if automated_analysis is not None:
            fetch_lineage_records_reconciled = _reconcile_openai_fetch_lineage(
                run_dir,
                phase3_observations,
            )
        generated_claims = _visual_claims_from_observations(evidence, now=now)
        evidence["claims"].extend(generated_claims)
        generated_claim_count = len(generated_claims)
        linkage_count = _link_visual_evidence_to_claims(evidence)
        adapter_status = "visual_evidence_ingested"
    else:
        missing_claims = _missing_visual_claims(evidence, now=now)
        evidence["claims"].extend(missing_claims)
        _write_jsonl(visual_observations_path, [])
        generated_claim_count = 0
        linkage_count = 0
        adapter_status = "needs_visual_evidence" if missing_claims else "no_visual_tasks"

    evidence["vision_adapter"] = {
        "schema_version": VISION_ADAPTER_SCHEMA_VERSION,
        "status": adapter_status,
        "provider": normalized_provider,
        "ingested_at": now,
        "input_observations_path": _relative_or_string(run_dir, input_path),
        "visual_observations_path": "visual_observations.jsonl",
        "images_ingested": len(normalized_images),
        "images_reused": images_reused,
        "observation_claims_created": generated_claim_count,
        "claim_visual_links_created": linkage_count,
        "missing_visual_claims_created": 0
        if normalized_images
        else len(
            [
                claim
                for claim in evidence["claims"]
                if isinstance(claim, Mapping)
                and claim.get("extraction_stage") == MISSING_VISUAL_STAGE
            ]
        ),
        "external_vlm_call": automated_analysis is not None,
    }
    if automated_analysis is not None:
        evidence["vision_adapter"].update(
            {
                "provider_mode": automated_analysis.provider_mode,
                "model_or_tool": automated_analysis.model,
                "vlm_images_analyzed": len(automated_analysis.records),
                "estimated_cost_usd": automated_analysis.estimated_cost_usd,
                "actual_cost_usd": automated_analysis.actual_cost_usd,
                "visual_provider_status_path": VISUAL_PROVIDER_STATUS_FILENAME,
                "fetch_lineage_records_reconciled": fetch_lineage_records_reconciled,
                "external_vlm_call": automated_analysis.external_vlm_call,
            }
        )
    handoff = evidence.get("handoff")
    if isinstance(handoff, dict):
        handoff["visual_observations_path"] = "visual_observations.jsonl"
        handoff["visual_status"] = adapter_status
        if automated_analysis is not None:
            handoff["visual_provider_status_path"] = VISUAL_PROVIDER_STATUS_FILENAME

    _write_json(evidence_path, evidence)
    validation = validate_artifacts(
        evidence_path=evidence_path,
        visual_observations_path=visual_observations_path,
    )
    if not validation.valid:
        adapter_status = "failed_validation"

    status = _base_status(run_dir, evidence, adapter_status)
    status.update(
        {
            "provider": normalized_provider,
            "images_ingested": len(normalized_images),
            "images_reused": images_reused,
            "observation_claims_created": generated_claim_count,
            "claim_visual_links_created": linkage_count,
            "missing_visual_claims_created": evidence["vision_adapter"][
                "missing_visual_claims_created"
            ],
            "validation": validation.to_dict(),
            "artifacts": {
                "evidence": str(evidence_path),
                "visual_observations": str(visual_observations_path),
                "vision_ingest_status": str(run_dir / "vision_ingest_status.json"),
            },
            "external_vlm_call": False,
        }
    )
    if automated_analysis is not None:
        status.update(
            {
                "provider_mode": automated_analysis.provider_mode,
                "model_or_tool": automated_analysis.model,
                "vlm_images_analyzed": len(automated_analysis.records),
                "estimated_cost_usd": automated_analysis.estimated_cost_usd,
                "actual_cost_usd": automated_analysis.actual_cost_usd,
                "external_vlm_call": automated_analysis.external_vlm_call,
                "fetch_lineage_records_reconciled": fetch_lineage_records_reconciled,
            }
        )
        status["artifacts"]["visual_provider_status"] = str(
            run_dir / VISUAL_PROVIDER_STATUS_FILENAME
        )
        _write_visual_provider_status(
            run_dir=run_dir,
            provider_status=automated_analysis.provider_status,
            status=_automated_analysis_provider_status(automated_analysis),
            ok=True,
            terminal=False,
            metric_classification="vlm_analysis",
            actionable_cause=_automated_analysis_actionable_cause(automated_analysis),
            created_at=now,
        )
        visual_artifact_validation = validate_visual_artifacts(
            run_dir=run_dir,
            evidence_path=None,
        )
        status["visual_artifact_validation"] = visual_artifact_validation.to_dict()
    record_stage_trace(
        run_dir,
        stage="ingest_vision",
        agent_role="vision_adapter",
        status_payload=status,
        prompt_summary="Normalize visual handoff records into VisualEvidence.",
        tool_call_summary=_vision_trace_tool_call_summary(automated_analysis),
    )
    _write_json(run_dir / "vision_ingest_status.json", status)
    return status


class _HttpOpenAIResponsesVisionClient:
    def analyze_image(
        self,
        *,
        image_input: str,
        mime_type: str,
        prompt: str,
        config: OpenAIResponsesVisionConfig,
        metadata: Mapping[str, Any],
    ) -> OpenAIResponsesVisionResult:
        if not config.api_key:
            raise VisionAdapterError("missing OpenAI API key")
        started = time.monotonic()
        payload = {
            "model": config.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {
                            "type": "input_image",
                            "image_url": image_input,
                            "detail": config.detail,
                        },
                    ],
                }
            ],
            "max_output_tokens": 900,
        }
        request = Request(
            config.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "codex-deepresearch/0.1",
            },
            method="POST",
        )
        with urlopen(request, timeout=config.timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        try:
            response_payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise VisionAdapterError(f"OpenAI Responses API returned invalid JSON: {exc}") from exc
        if not isinstance(response_payload, Mapping):
            raise VisionAdapterError("OpenAI Responses API returned a non-object payload")
        return _openai_result_from_response_payload(
            response_payload,
            fallback_model=config.model,
            elapsed_ms=elapsed_ms,
            mime_type=mime_type,
            metadata=metadata,
        )


class _SubprocessCodexInteractiveVisionClient:
    def analyze_image(
        self,
        *,
        image_path: Path,
        mime_type: str,
        prompt: str,
        config: CodexInteractiveVisionConfig,
        metadata: Mapping[str, Any],
    ) -> OpenAIResponsesVisionResult:
        command = [
            config.codex_binary,
            "exec",
            "--json",
            "--ignore-user-config",
            "--ignore-rules",
            "-C",
            str(config.project_root),
            "--add-dir",
            str(Path(str(metadata.get("run_dir") or image_path.parent)).resolve()),
            "--image",
            str(image_path.resolve()),
        ]
        completed = subprocess.run(
            command,
            cwd=config.project_root,
            check=False,
            capture_output=True,
            input=prompt,
            text=True,
            timeout=config.timeout_seconds,
        )
        if completed.returncode != 0:
            diagnostic = (
                f"codex exec exited {completed.returncode}; "
                f"stderr={_preview_text(completed.stderr)}; "
                f"stdout={_preview_text(completed.stdout)}"
            )
            raise VisionAdapterError(_redact_provider_text(diagnostic, config=None))
        return _codex_result_from_stdout(
            completed.stdout,
            fallback_model=config.model,
            mime_type=mime_type,
            metadata=metadata,
        )


def _openai_result_from_response_payload(
    payload: Mapping[str, Any],
    *,
    fallback_model: str,
    elapsed_ms: int,
    mime_type: str,
    metadata: Mapping[str, Any],
) -> OpenAIResponsesVisionResult:
    output_text = _first_optional_string(payload, "output_text") or _response_output_text(payload)
    parsed = _parse_openai_response_text(output_text)
    usage = payload.get("usage")
    raw_metadata = {
        "response_id": _first_optional_string(payload, "id"),
        "model": _first_optional_string(payload, "model") or fallback_model,
        "usage": dict(usage) if isinstance(usage, Mapping) else {},
        "elapsed_ms": elapsed_ms,
        "mime_type": mime_type,
        "lineage": {
            key: metadata.get(key)
            for key in (
                "candidate_id",
                "fetch_id",
                "plan_id",
                "task_id",
                "angle_id",
                "route",
                "evidence_image_id",
            )
            if metadata.get(key) is not None
        },
    }
    if not parsed["observations"] and not parsed["inferences"] and output_text:
        parsed["inferences"] = [output_text]
        parsed["caveats"].append(
            "Provider returned text without separated observations; adapter preserved it as a caveated inference."
        )
    return OpenAIResponsesVisionResult(
        observations=tuple(parsed["observations"]),
        inferences=tuple(parsed["inferences"]),
        caveats=tuple(parsed["caveats"]),
        ocr_text=parsed["ocr_text"],
        confidence=parsed["confidence"],
        response_id=_first_optional_string(payload, "id"),
        model=_first_optional_string(payload, "model") or fallback_model,
        usage=dict(usage) if isinstance(usage, Mapping) else {},
        raw_provider_metadata=raw_metadata,
        actual_cost_usd=None,
    )


def _codex_result_from_stdout(
    stdout: str,
    *,
    fallback_model: str,
    mime_type: str,
    metadata: Mapping[str, Any],
) -> OpenAIResponsesVisionResult:
    candidates = _codex_stdout_candidate_texts(stdout)
    for candidate in candidates:
        if not _looks_like_json_object_text(candidate):
            continue
        parsed = _parse_openai_response_text(candidate)
        if parsed["observations"] or parsed["inferences"] or parsed["ocr_text"]:
            return OpenAIResponsesVisionResult(
                observations=tuple(parsed["observations"]),
                inferences=tuple(parsed["inferences"]),
                caveats=tuple(parsed["caveats"]),
                ocr_text=parsed["ocr_text"],
                confidence=parsed["confidence"],
                response_id=None,
                model=fallback_model,
                usage={},
                raw_provider_metadata={
                    "mime_type": mime_type,
                    "lineage": {
                        key: metadata.get(key)
                        for key in (
                            "candidate_id",
                            "fetch_id",
                            "plan_id",
                            "task_id",
                            "angle_id",
                            "route",
                            "evidence_image_id",
                            "local_artifact_path",
                        )
                        if metadata.get(key) is not None
                    },
                    "output_format": "codex-exec-json",
                },
                actual_cost_usd=0.0,
            )
    raise VisionAdapterError("codex exec did not return schema-valid visual JSON")


def _looks_like_json_object_text(text: str) -> bool:
    stripped = _strip_json_object_text(text)
    return stripped.startswith("{") and stripped.endswith("}")


def _strip_json_object_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
        stripped = stripped.strip()
    return stripped


def _codex_stdout_candidate_texts(stdout: str) -> list[str]:
    candidates: list[str] = []
    if _looks_like_json_object_text(stdout):
        try:
            parsed_stdout = json.loads(_strip_json_object_text(stdout))
        except json.JSONDecodeError:
            parsed_stdout = None
        if isinstance(parsed_stdout, Mapping):
            candidates.append(stdout.strip())
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        candidates.extend(_codex_payload_texts(payload))
    return _dedupe(candidates)


def _codex_payload_texts(payload: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(payload, str):
        texts.append(payload)
        return texts
    if isinstance(payload, list):
        for item in payload:
            texts.extend(_codex_payload_texts(item))
        return texts
    if not isinstance(payload, Mapping):
        return texts
    if "observations" in payload or "inferences" in payload or "ocr_text" in payload:
        texts.append(json.dumps(payload, sort_keys=True))
    for key in ("output_text", "text", "message", "content", "delta"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())
        elif isinstance(value, (Mapping, list)):
            texts.extend(_codex_payload_texts(value))
    for key in ("output", "item", "result", "data"):
        value = payload.get(key)
        if isinstance(value, (Mapping, list)):
            texts.extend(_codex_payload_texts(value))
    return texts


def _parse_openai_response_text(text: str | None) -> dict[str, Any]:
    result = {
        "observations": [],
        "inferences": [],
        "caveats": [],
        "ocr_text": None,
        "confidence": 0.7,
    }
    if not text:
        result["caveats"].append("Provider response had no output_text.")
        return result
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        result["inferences"].append(stripped)
        result["caveats"].append(
            "Provider response was not JSON; free-form text was not promoted to observations."
        )
        return result
    if not isinstance(parsed, Mapping):
        result["inferences"].append(stripped)
        result["caveats"].append(
            "Provider JSON was not an object; free-form text was not promoted to observations."
        )
        return result
    result["observations"] = _dedupe(_string_list(parsed, "observations"))
    result["inferences"] = _dedupe(_string_list(parsed, "inferences"))
    result["caveats"] = _dedupe(_string_list(parsed, "caveats"))
    result["ocr_text"] = _first_optional_string(parsed, "ocr_text", "text_in_image")
    confidence = parsed.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        result["confidence"] = max(0.0, min(1.0, float(confidence)))
    elif isinstance(confidence, str) and confidence.strip():
        try:
            result["confidence"] = max(0.0, min(1.0, float(confidence)))
        except ValueError:
            result["caveats"].append(f"Unparsed provider confidence: {confidence}")
    return result


def _response_output_text(payload: Mapping[str, Any]) -> str | None:
    output = payload.get("output")
    parts: list[str] = []
    if not isinstance(output, list):
        return None
    for item in output:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping):
                continue
            text = _first_optional_string(part, "text")
            if text:
                parts.append(text)
    return "\n".join(parts) if parts else None


def _run_openai_responses_vision_analysis(
    *,
    run_dir: Path,
    evidence: dict[str, Any],
    now: str,
    provider_mode: str | None,
    allow_real_vlm: bool | None,
    openai_config: Mapping[str, Any] | None,
    openai_client: OpenAIResponsesVisionClient | None,
) -> _AutomatedVisionAnalysis | dict[str, Any]:
    config = _openai_responses_vision_config(
        overrides=openai_config,
        provider_mode=provider_mode,
        allow_real_vlm=allow_real_vlm,
        evidence=evidence,
    )
    tasks = _openai_vision_tasks(run_dir=run_dir, evidence=evidence, max_images=config.max_images)
    if not tasks:
        return _write_blocked_missing_vlm_provider(
            run_dir=run_dir,
            evidence=evidence,
            status_reason="no_fetched_visual_artifacts",
            configured=bool(config.api_key),
            available=False,
            provider_mode=config.provider_mode,
            model=config.model,
            created_at=now,
            task_count=0,
        )
    block_reason = _openai_vlm_block_reason(config)
    if block_reason is not None:
        return _write_blocked_missing_vlm_provider(
            run_dir=run_dir,
            evidence=evidence,
            status_reason=block_reason,
            configured=bool(config.api_key),
            available=False,
            provider_mode=config.provider_mode,
            model=config.model,
            created_at=now,
            task_count=len(tasks),
        )
    _ensure_openai_task_sources(run_dir, evidence, tasks, created_at=now)

    client = openai_client or _HttpOpenAIResponsesVisionClient()
    records: list[dict[str, Any]] = []
    total_estimated = 0.0
    total_actual = 0.0
    provider_run_id = f"openai-responses-vision:{run_dir.name}:{_sanitize_identifier(now)}"
    for index, task in enumerate(tasks, start=1):
        image_input = _openai_image_input(run_dir, task)
        prompt = _openai_vision_prompt(task)
        estimated_cost = _estimated_cost_for_task(task, config=config)
        metadata = {
            "candidate_id": task["candidate_id"],
            "fetch_id": task["fetch_id"],
            "task_id": task["task_id"],
            "angle_id": task["angle_id"],
            "evidence_image_id": task["evidence_image_id"],
        }
        try:
            result = client.analyze_image(
                image_input=image_input,
                mime_type=str(task["mime_type"]),
                prompt=prompt,
                config=config,
                metadata=metadata,
            )
        except Exception as exc:
            partial_observations_path = None
            if records:
                partial_observations_path = run_dir / OPENAI_RESPONSES_VISION_RESPONSE_FILENAME
                _write_jsonl(partial_observations_path, records)
            return _write_blocked_missing_vlm_provider(
                run_dir=run_dir,
                evidence=evidence,
                status_reason="provider_request_failed",
                configured=True,
                available=False,
                provider_mode=config.provider_mode,
                model=config.model,
                created_at=now,
                task_count=len(tasks),
                last_error=_redact_provider_text(str(exc) or exc.__class__.__name__, config=config),
                provider_run_id=provider_run_id,
                invocations=index,
                vlm_images_analyzed=len(records),
                estimated_cost_usd=round(total_estimated + float(estimated_cost), 8),
                actual_cost_usd=round(total_actual, 8),
                external_vlm_call=True,
                partial_observations_path=partial_observations_path,
            )
        actual_cost = (
            result.actual_cost_usd
            if result.actual_cost_usd is not None
            else config.actual_cost_usd_per_image
        )
        total_estimated += float(estimated_cost)
        total_actual += float(actual_cost)
        records.append(
            _openai_observation_record(
                task,
                result=result,
                config=config,
                provider_run_id=provider_run_id,
                provider_mode=config.provider_mode,
                model=config.model,
                estimated_cost_usd=estimated_cost,
                actual_cost_usd=actual_cost,
                created_at=now,
                sequence=index,
            )
        )

    observations_path = run_dir / OPENAI_RESPONSES_VISION_RESPONSE_FILENAME
    _write_jsonl(observations_path, records)
    provider_status = {
        "provider": OPENAI_RESPONSES_VISION_PROVIDER,
        "provider_kind": "vlm",
        "provider_mode": config.provider_mode,
        "provider_run_id": provider_run_id,
        "configured": True,
        "available": True,
        "blocked_reason": None,
        "invoked": True,
        "invocations": len(records),
        "candidates_discovered": 0,
        "artifacts_fetched": len(tasks),
        "vlm_images_analyzed": len(records),
        "estimated_cost_usd": round(total_estimated, 8),
        "actual_cost_usd": round(total_actual, 8),
        "last_error": None,
        "external_network_call": True,
        "external_vlm_call": True,
        "diagnostics": {
            "model": config.model,
            "detail": config.detail,
            "endpoint": _sanitize_provider_url(config.endpoint),
            "allow_env": OPENAI_RESPONSES_VISION_ALLOW_ENV,
            "mode_env": OPENAI_RESPONSES_VISION_MODE_ENV,
            "records_written": len(records),
        },
    }
    return _AutomatedVisionAnalysis(
        records=tuple(records),
        observations_path=observations_path,
        provider_status=provider_status,
        estimated_cost_usd=round(total_estimated, 8),
        actual_cost_usd=round(total_actual, 8),
        model=config.model,
        provider_mode=config.provider_mode,
        provider=OPENAI_RESPONSES_VISION_PROVIDER,
        external_vlm_call=True,
    )


def _run_codex_interactive_vision_analysis(
    *,
    run_dir: Path,
    evidence: dict[str, Any],
    now: str,
    provider_mode: str | None,
    codex_config: Mapping[str, Any] | None,
    codex_client: CodexInteractiveVisionClient | None,
) -> _AutomatedVisionAnalysis | dict[str, Any]:
    config = _codex_interactive_vision_config(
        overrides=codex_config,
        provider_mode=provider_mode,
        evidence=evidence,
    )
    tasks = _codex_interactive_vision_tasks(
        run_dir=run_dir,
        evidence=evidence,
        max_images=config.max_images,
    )
    if not tasks:
        return _write_blocked_missing_codex_vlm_provider(
            run_dir=run_dir,
            evidence=evidence,
            status_reason="no_fetched_visual_artifacts",
            configured=True,
            available=False,
            provider_mode=config.provider_mode,
            model=config.model,
            created_at=now,
            task_count=0,
        )
    if config.provider_mode != "real":
        return _write_blocked_missing_codex_vlm_provider(
            run_dir=run_dir,
            evidence=evidence,
            status_reason="codex_interactive_mode_not_real",
            configured=True,
            available=False,
            provider_mode=config.provider_mode,
            model=config.model,
            created_at=now,
            task_count=len(tasks),
        )
    if codex_client is None and shutil.which(config.codex_binary) is None:
        return _write_blocked_missing_codex_vlm_provider(
            run_dir=run_dir,
            evidence=evidence,
            status_reason="codex_exec_unavailable",
            configured=True,
            available=False,
            provider_mode=config.provider_mode,
            model=config.model,
            created_at=now,
            task_count=len(tasks),
        )

    client = codex_client or _SubprocessCodexInteractiveVisionClient()
    records: list[dict[str, Any]] = []
    provider_run_id = f"codex-interactive:{run_dir.name}:{_sanitize_identifier(now)}"
    for index, task in enumerate(tasks, start=1):
        image_path = _codex_image_path(run_dir, task)
        prompt = _codex_interactive_vision_prompt(task)
        metadata = {
            "candidate_id": task["candidate_id"],
            "fetch_id": task["fetch_id"],
            "task_id": task["task_id"],
            "angle_id": task["angle_id"],
            "evidence_image_id": task["evidence_image_id"],
            "local_artifact_path": task.get("local_artifact_path"),
            "run_dir": str(run_dir),
        }
        try:
            result = client.analyze_image(
                image_path=image_path,
                mime_type=str(task["mime_type"]),
                prompt=prompt,
                config=config,
                metadata=metadata,
            )
        except Exception as exc:
            partial_observations_path = None
            if records:
                partial_observations_path = run_dir / CODEX_INTERACTIVE_RESPONSE_FILENAME
                _write_jsonl(partial_observations_path, records)
            return _write_blocked_missing_codex_vlm_provider(
                run_dir=run_dir,
                evidence=evidence,
                status_reason="provider_request_failed",
                configured=True,
                available=False,
                provider_mode=config.provider_mode,
                model=config.model,
                created_at=now,
                task_count=len(tasks),
                last_error=_redact_provider_text(str(exc) or exc.__class__.__name__, config=None),
                provider_run_id=provider_run_id,
                invocations=index,
                vlm_images_analyzed=len(records),
                partial_observations_path=partial_observations_path,
            )
        records.append(
            _codex_interactive_observation_record(
                task,
                result=result,
                provider_run_id=provider_run_id,
                provider_mode=config.provider_mode,
                model=config.model,
                created_at=now,
                sequence=index,
            )
        )

    observations_path = run_dir / CODEX_INTERACTIVE_RESPONSE_FILENAME
    _write_jsonl(observations_path, records)
    provider_status = {
        "provider": CODEX_INTERACTIVE_PROVIDER,
        "provider_kind": "vlm",
        "provider_mode": config.provider_mode,
        "provider_run_id": provider_run_id,
        "configured": True,
        "available": True,
        "blocked_reason": None,
        "invoked": True,
        "invocations": len(records),
        "candidates_discovered": 0,
        "artifacts_fetched": len(tasks),
        "vlm_images_analyzed": len(records),
        "estimated_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
        "last_error": None,
        "external_network_call": False,
        "external_vlm_call": False,
        "codex_native_handoff": True,
        "codex_interactive_handoff": True,
        "handoff_recorded": True,
        "handoff_artifact": "visual_observations.jsonl",
        "explicit_artifact_handoff": True,
        "hidden_codex_api_call": False,
        "diagnostics": {
            "model": config.model,
            "worker_command": "codex exec --json --image <artifact>",
            "records_written": len(records),
            "fetched_artifacts_preserved": True,
        },
    }
    return _AutomatedVisionAnalysis(
        records=tuple(records),
        observations_path=observations_path,
        provider_status=provider_status,
        estimated_cost_usd=0.0,
        actual_cost_usd=0.0,
        model=config.model,
        provider_mode=config.provider_mode,
        provider=CODEX_INTERACTIVE_PROVIDER,
        external_vlm_call=False,
    )


def _openai_responses_vision_config(
    *,
    overrides: Mapping[str, Any] | None,
    provider_mode: str | None,
    allow_real_vlm: bool | None,
    evidence: Mapping[str, Any],
) -> OpenAIResponsesVisionConfig:
    mode = (
        provider_mode
        or _config_string(overrides, "provider_mode", env_names=(OPENAI_RESPONSES_VISION_MODE_ENV,))
        or "fixture"
    )
    normalized_mode = mode.strip().lower().replace("_", "-")
    if normalized_mode not in {"real", "fixture", "manual", "user-provided"}:
        raise VisionAdapterError(
            "provider_mode must be one of: real, fixture, manual, user_provided"
        )
    if normalized_mode == "user-provided":
        normalized_mode = "user_provided"
    configured_allow = (
        allow_real_vlm
        if allow_real_vlm is not None
        else _config_bool(
            overrides,
            "allow_real_vlm",
            env_names=(OPENAI_RESPONSES_VISION_ALLOW_ENV,),
            default=False,
        )
    )
    max_images = _config_int(
        overrides,
        "max_images",
        env_names=(OPENAI_RESPONSES_VISION_MAX_IMAGES_ENV,),
        default=None,
    )
    if max_images is None:
        budget = evidence.get("budget")
        if isinstance(budget, Mapping):
            value = budget.get("max_images")
            if isinstance(value, int) and value > 0:
                max_images = value
    return OpenAIResponsesVisionConfig(
        api_key=_config_string(
            overrides,
            "api_key",
            env_names=("CODEX_DEEPRESEARCH_OPENAI_API_KEY", "OPENAI_API_KEY"),
        ),
        endpoint=_config_string(
            overrides,
            "endpoint",
            env_names=("CODEX_DEEPRESEARCH_OPENAI_RESPONSES_ENDPOINT",),
            default=OPENAI_RESPONSES_VISION_ENDPOINT,
        )
        or OPENAI_RESPONSES_VISION_ENDPOINT,
        model=_config_string(
            overrides,
            "model",
            env_names=(OPENAI_RESPONSES_VISION_MODEL_ENV,),
            default=OPENAI_RESPONSES_VISION_MODEL,
        )
        or OPENAI_RESPONSES_VISION_MODEL,
        detail=_config_string(
            overrides,
            "detail",
            env_names=(OPENAI_RESPONSES_VISION_DETAIL_ENV,),
            default="auto",
        )
        or "auto",
        timeout_seconds=_config_float(
            overrides,
            "timeout_seconds",
            env_names=(OPENAI_RESPONSES_VISION_TIMEOUT_ENV,),
            default=60.0,
        ),
        allow_real_vlm=bool(configured_allow),
        provider_mode=normalized_mode,
        max_images=max_images,
        estimated_cost_usd_per_image=_config_float(
            overrides,
            "estimated_cost_usd_per_image",
            env_names=(OPENAI_RESPONSES_VISION_ESTIMATED_COST_ENV,),
            default=0.0,
        ),
        actual_cost_usd_per_image=_config_float(
            overrides,
            "actual_cost_usd_per_image",
            env_names=(OPENAI_RESPONSES_VISION_ACTUAL_COST_ENV,),
            default=0.0,
        ),
    )


def _codex_interactive_vision_config(
    *,
    overrides: Mapping[str, Any] | None,
    provider_mode: str | None,
    evidence: Mapping[str, Any],
) -> CodexInteractiveVisionConfig:
    mode = (
        provider_mode
        or _config_string(overrides, "provider_mode")
        or "real"
    )
    normalized_mode = mode.strip().lower().replace("_", "-")
    if normalized_mode not in {"real", "fixture", "manual", "user-provided"}:
        raise VisionAdapterError(
            "provider_mode must be one of: real, fixture, manual, user_provided"
        )
    if normalized_mode == "user-provided":
        normalized_mode = "user_provided"
    max_images = _config_int(
        overrides,
        "max_images",
        env_names=(CODEX_INTERACTIVE_MAX_IMAGES_ENV,),
        default=None,
    )
    if max_images is None:
        budget = evidence.get("budget")
        if isinstance(budget, Mapping):
            value = budget.get("max_images")
            if isinstance(value, int) and value > 0:
                max_images = value
    project_root_text = _config_string(overrides, "project_root")
    return CodexInteractiveVisionConfig(
        codex_binary=_config_string(
            overrides,
            "codex_binary",
            env_names=(CODEX_INTERACTIVE_BINARY_ENV,),
            default="codex",
        )
        or "codex",
        timeout_seconds=_config_float(
            overrides,
            "timeout_seconds",
            env_names=(CODEX_INTERACTIVE_TIMEOUT_ENV,),
            default=120.0,
        ),
        provider_mode=normalized_mode,
        max_images=max_images,
        model=_config_string(overrides, "model", default=CODEX_INTERACTIVE_MODEL)
        or CODEX_INTERACTIVE_MODEL,
        project_root=Path(project_root_text).resolve()
        if project_root_text
        else Path(__file__).resolve().parents[4],
    )


def _openai_vlm_block_reason(config: OpenAIResponsesVisionConfig) -> str | None:
    if config.provider_mode != "real":
        return "openai_responses_vision_mode_not_real"
    if not config.allow_real_vlm:
        return "real_vlm_not_allowed"
    if not config.api_key:
        return "missing_openai_api_key"
    return None


def _openai_vision_tasks(
    *,
    run_dir: Path,
    evidence: Mapping[str, Any],
    max_images: int | None,
) -> list[dict[str, Any]]:
    candidates = [
        item
        for item in _read_jsonl(run_dir / "visual_candidates.jsonl")
        if isinstance(item, Mapping)
    ]
    fetches = [
        item
        for item in _read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        if isinstance(item, Mapping)
    ]
    candidates_by_id = {
        str(candidate.get("candidate_id") or candidate.get("id")): candidate
        for candidate in candidates
        if candidate.get("candidate_id") or candidate.get("id")
    }
    fetches_by_candidate_id: dict[str, list[Mapping[str, Any]]] = {}
    for fetch in fetches:
        candidate_id = _first_optional_string(fetch, "candidate_id")
        if candidate_id:
            fetches_by_candidate_id.setdefault(candidate_id, []).append(fetch)
    tasks: list[dict[str, Any]] = []
    for fetch in fetches:
        if fetch.get("fetch_status") != "fetched":
            continue
        if fetch.get("policy_decision") in {"blocked", "budget_pruned"}:
            continue
        candidate_id = _first_optional_string(fetch, "candidate_id")
        candidate = candidates_by_id.get(candidate_id or "")
        task = _task_from_fetch(run_dir, fetch, candidate, evidence=evidence)
        if task is not None:
            tasks.append(task)
    fetched_candidate_ids = {task["candidate_id"] for task in tasks}
    for candidate in candidates:
        candidate_id = _first_optional_string(candidate, "candidate_id", "id")
        if not candidate_id or candidate_id in fetched_candidate_ids:
            continue
        if candidate.get("policy_decision") in {"blocked", "budget_pruned"}:
            continue
        if not _candidate_allows_openai_url_analysis(
            candidate,
            fetches=fetches_by_candidate_id.get(candidate_id, []),
        ):
            continue
        task = _task_from_url_candidate(run_dir, candidate, evidence=evidence)
        if task is not None:
            tasks.append(task)
    return tasks[:max_images] if max_images is not None and max_images > 0 else tasks


def _codex_interactive_vision_tasks(
    *,
    run_dir: Path,
    evidence: Mapping[str, Any],
    max_images: int | None,
) -> list[dict[str, Any]]:
    tasks = []
    for task in _openai_vision_tasks(run_dir=run_dir, evidence=evidence, max_images=None):
        local_path = _first_optional_string(task, "local_artifact_path")
        if not local_path:
            continue
        artifact = _resolve_run_relative_path(run_dir, local_path)
        if artifact.is_file():
            tasks.append(task)
    return tasks[:max_images] if max_images is not None and max_images > 0 else tasks


def _candidate_allows_openai_url_analysis(
    candidate: Mapping[str, Any],
    *,
    fetches: Sequence[Mapping[str, Any]],
) -> bool:
    if fetches:
        return False
    status = _first_optional_string(candidate, "candidate_status") or "discovered"
    if status not in {"discovered", "ranked", "selected", "fetched"}:
        return False
    if _first_optional_string(candidate, "status") == "removed":
        return False
    if _string_list(candidate, "removal_reasons"):
        return False
    return True


def _task_from_fetch(
    run_dir: Path,
    fetch: Mapping[str, Any],
    candidate: Mapping[str, Any] | None,
    *,
    evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    candidate_id = _first_optional_string(fetch, "candidate_id")
    if not candidate_id:
        return None
    local_artifact_path = _first_optional_string(fetch, "local_artifact_path")
    artifact_file = (
        _resolve_run_relative_path(run_dir, local_artifact_path)
        if local_artifact_path
        else None
    )
    if artifact_file is None or not artifact_file.is_file():
        return None
    mime_type = _first_optional_string(fetch, "mime_type") or _first_optional_string(
        candidate or {},
        "mime_type",
    )
    if mime_type not in OPENAI_RESPONSES_VISION_SUPPORTED_MIME_TYPES:
        return None
    evidence_image_id = _first_optional_string(fetch, "evidence_image_id") or _image_id_for_candidate_id(
        candidate_id
    )
    return _base_openai_task(
        run_dir=run_dir,
        candidate=candidate,
        fetch=fetch,
        evidence=evidence,
        candidate_id=candidate_id,
        fetch_id=_first_optional_string(fetch, "fetch_id")
        or _fetch_id_for_candidate_id(candidate_id),
        evidence_image_id=evidence_image_id,
        local_artifact_path=local_artifact_path,
        image_url=_first_optional_string(candidate or {}, "image_url"),
        page_url=_first_optional_string(candidate or {}, "page_url"),
        mime_type=mime_type,
        width=_task_number(fetch, candidate, "width"),
        height=_task_number(fetch, candidate, "height"),
        artifact_size_bytes=_task_number(fetch, candidate, "byte_size", "artifact_size_bytes"),
        image_hash=_first_optional_string(fetch, "hash") or _first_optional_string(
            candidate or {},
            "hash",
        ),
        phash=_first_optional_string(fetch, "phash") or _first_optional_string(
            candidate or {},
            "phash",
        ),
    )


def _task_from_url_candidate(
    run_dir: Path,
    candidate: Mapping[str, Any],
    *,
    evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    candidate_id = _first_optional_string(candidate, "candidate_id", "id")
    image_url = _first_optional_string(candidate, "image_url")
    if not candidate_id or not image_url:
        return None
    parsed = urlparse(image_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return _base_openai_task(
        run_dir=run_dir,
        candidate=candidate,
        fetch=None,
        evidence=evidence,
        candidate_id=candidate_id,
        fetch_id=_fetch_id_for_candidate_id(candidate_id),
        evidence_image_id=_image_id_for_candidate_id(candidate_id),
        local_artifact_path=None,
        image_url=image_url,
        page_url=_first_optional_string(candidate, "page_url"),
        mime_type=_first_optional_string(candidate, "mime_type") or "image/unknown",
        width=_task_number(candidate, None, "width"),
        height=_task_number(candidate, None, "height"),
        artifact_size_bytes=_task_number(candidate, None, "artifact_size_bytes", "byte_size"),
        image_hash=_first_optional_string(candidate, "hash"),
        phash=_first_optional_string(candidate, "phash"),
    )


def _base_openai_task(
    *,
    run_dir: Path,
    candidate: Mapping[str, Any] | None,
    fetch: Mapping[str, Any] | None,
    evidence: Mapping[str, Any],
    candidate_id: str,
    fetch_id: str,
    evidence_image_id: str,
    local_artifact_path: str | None,
    image_url: str | None,
    page_url: str | None,
    mime_type: str,
    width: int | float,
    height: int | float,
    artifact_size_bytes: int | float,
    image_hash: str | None,
    phash: str | None,
) -> dict[str, Any]:
    source_id = _first_optional_string(candidate or {}, "source_id") or _first_optional_string(
        fetch or {},
        "source_id",
    )
    angle_id = _first_optional_string(fetch or {}, "angle_id") or _first_optional_string(
        candidate or {},
        "angle_id",
    ) or "angle_001"
    task_id = _first_optional_string(fetch or {}, "task_id") or _first_optional_string(
        candidate or {},
        "task_id",
    ) or f"task_visual_{_sanitize_identifier(angle_id.removeprefix('angle_'))}"
    route = _first_optional_string(fetch or {}, "route") or _first_optional_string(
        candidate or {},
        "route",
    )
    plan_id = _first_optional_string(fetch or {}, "plan_id") or _first_optional_string(
        candidate or {},
        "plan_id",
    ) or _plan_id_for_visual_task(
        task_id=task_id,
        angle_id=angle_id,
        route=route or "visual_required",
    )
    return {
        "candidate_id": candidate_id,
        "fetch_id": fetch_id,
        "evidence_image_id": evidence_image_id,
        "plan_id": plan_id,
        "source_id": source_id,
        "origin": _first_optional_string(candidate or {}, "origin") or "image_search",
        "image_url": image_url,
        "page_url": page_url,
        "local_artifact_path": local_artifact_path,
        "mime_type": mime_type,
        "width": width,
        "height": height,
        "artifact_size_bytes": artifact_size_bytes,
        "hash": image_hash,
        "phash": phash,
        "task_id": task_id,
        "angle_id": angle_id,
        "route": route,
        "candidate_class": _first_optional_string(candidate or {}, "candidate_class"),
        "visual_tasks": _dedupe(_string_list(candidate or {}, "visual_tasks")),
        "policy_decision": _first_optional_string(fetch or {}, "policy_decision")
        or _first_optional_string(candidate or {}, "policy_decision")
        or "allowed",
        "policy_flags": _dedupe(
            _string_list(fetch or {}, "policy_flags")
            + _string_list(candidate or {}, "policy_flags")
        ),
        "visual_validation": dict(candidate.get("validation_checks", {}))
        if isinstance(candidate, Mapping) and isinstance(candidate.get("validation_checks"), Mapping)
        else {},
        "source_url": _source_url_for_task(evidence, source_id),
        "raw_candidate": dict(candidate) if isinstance(candidate, Mapping) else {},
        "raw_fetch": dict(fetch) if isinstance(fetch, Mapping) else {},
        "run_dir": str(run_dir),
    }


def _openai_image_input(run_dir: Path, task: Mapping[str, Any]) -> str:
    local_path = _first_optional_string(task, "local_artifact_path")
    if local_path:
        artifact = _resolve_run_relative_path(run_dir, local_path)
        if artifact.is_file():
            mime_type = _first_optional_string(task, "mime_type") or "image/png"
            encoded = base64.b64encode(artifact.read_bytes()).decode("ascii")
            return f"data:{mime_type};base64,{encoded}"
    image_url = _first_optional_string(task, "image_url")
    if image_url:
        return image_url
    raise VisionAdapterError("openai-responses-vision task requires local_artifact_path or image_url")


def _codex_image_path(run_dir: Path, task: Mapping[str, Any]) -> Path:
    local_path = _first_optional_string(task, "local_artifact_path")
    if not local_path:
        raise VisionAdapterError("codex-interactive task requires local_artifact_path")
    artifact = _resolve_run_relative_path(run_dir, local_path)
    if not artifact.is_file():
        raise VisionAdapterError(
            f"codex-interactive local_artifact_path does not exist: {local_path}"
        )
    return artifact


def _openai_vision_prompt(task: Mapping[str, Any]) -> str:
    visual_tasks = ", ".join(_string_list(task, "visual_tasks")) or "visual observation"
    return (
        "Analyze the image for a research evidence record. Return only JSON with keys "
        "observations, inferences, ocr_text, caveats, and confidence. "
        "Put directly visible facts, OCR text, chart values, and layout/object descriptions "
        "in observations[]. Put interpretations, likely implications, and claim alignment in "
        "inferences[]. Caveat uncertainty, approximate counts, unreadable text, and any limits. "
        f"Visual task focus: {visual_tasks}."
    )


def _codex_interactive_vision_prompt(task: Mapping[str, Any]) -> str:
    visual_tasks = ", ".join(_string_list(task, "visual_tasks")) or "visual observation"
    return (
        "Inspect the attached image artifact for a DeepResearch visual evidence record. "
        "The worker was invoked with codex exec --json --image. "
        "Return only one JSON object with this exact shape: "
        "{\"observations\":[\"directly visible facts or OCR text\"],"
        "\"inferences\":[\"interpretations or claim alignment\"],"
        "\"ocr_text\":\"visible text or null\","
        "\"caveats\":[\"uncertainty or limits\"],"
        "\"confidence\":0.0}. "
        "Do not include markdown, citations, or prose outside the JSON object. "
        "Use observations only for facts visible in the image. "
        f"Visual task focus: {visual_tasks}. "
        f"Evidence image id: {task['evidence_image_id']}; "
        f"candidate id: {task['candidate_id']}; fetch id: {task['fetch_id']}."
    )


def _ensure_openai_task_sources(
    run_dir: Path,
    evidence: dict[str, Any],
    tasks: Sequence[dict[str, Any]],
    *,
    created_at: str,
) -> None:
    sources = evidence.setdefault("sources", [])
    if not isinstance(sources, list):
        raise VisionAdapterError("evidence.sources must be a list")
    source_ids = _existing_ids(sources)
    for task in tasks:
        if _first_optional_string(task, "source_id"):
            continue
        source_url = _first_optional_string(task, "page_url") or _first_optional_string(
            task,
            "image_url",
        )
        if not source_url:
            continue
        source_id = _openai_generated_source_id(task, source_ids)
        task["source_id"] = source_id
        task["source_url"] = source_url
        source_ids.add(source_id)
        local_artifact_path = f"sources/{source_id}.json"
        source = {
            "id": source_id,
            "type": "web" if _first_optional_string(task, "page_url") else "image",
            "url": source_url,
            "title": f"Visual candidate {task['candidate_id']}",
            "published_at": None,
            "accessed_at": created_at,
            "quality": "unknown",
            "retrieval_status": "manual",
            "local_artifact_path": local_artifact_path,
            "license_policy": "manual_review",
            "robots_policy": "manual_review",
            "policy_decision": "manual_review",
            "policy_flags": ["openai_vision_generated_source"],
        }
        sources.append(source)
        metadata = {
            "schema_version": VISION_ADAPTER_SCHEMA_VERSION,
            "source_id": source_id,
            "candidate_id": task["candidate_id"],
            "fetch_id": task["fetch_id"],
            "image_url": task.get("image_url"),
            "page_url": task.get("page_url"),
            "created_at": created_at,
            "note": "Generated source metadata for URL-only openai-responses-vision analysis.",
        }
        source_path = run_dir / local_artifact_path
        source_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(source_path, metadata)


def _openai_generated_source_id(
    task: Mapping[str, Any],
    existing_ids: set[str],
) -> str:
    raw_candidate_id = str(task.get("candidate_id") or "visual")
    base = "src_" + _sanitize_identifier(raw_candidate_id.removeprefix("cand_"))
    candidate = base
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _openai_observation_record(
    task: Mapping[str, Any],
    *,
    result: OpenAIResponsesVisionResult,
    config: OpenAIResponsesVisionConfig,
    provider_run_id: str,
    provider_mode: str,
    model: str,
    estimated_cost_usd: float,
    actual_cost_usd: float,
    created_at: str,
    sequence: int,
) -> dict[str, Any]:
    observations = _dedupe(
        [
            _redact_provider_text(str(item).strip(), config=config)
            for item in result.observations
            if str(item).strip()
        ]
    )
    inferences = _dedupe(
        [
            _redact_provider_text(str(item).strip(), config=config)
            for item in result.inferences
            if str(item).strip()
        ]
    )
    caveats = _dedupe(
        [
            "OpenAI Responses vision output is model-generated; use observations as extracted visual evidence and treat inferences as interpretation.",
            *[
                _redact_provider_text(str(item).strip(), config=config)
                for item in result.caveats
                if str(item).strip()
            ],
        ]
    )
    raw_metadata = (
        _redact_provider_value(result.raw_provider_metadata, config=config)
        if isinstance(result.raw_provider_metadata, Mapping)
        else {}
    )
    ocr_text = (
        _redact_provider_text(result.ocr_text, config=config)
        if isinstance(result.ocr_text, str)
        else None
    )
    image_record = {
        "id": task["evidence_image_id"],
        "source_id": task.get("source_id"),
        "origin": task.get("origin") or "image_search",
        "image_url": task.get("image_url"),
        "page_url": task.get("page_url"),
        "mime_type": task.get("mime_type") or "image/unknown",
        "width": task.get("width", 0),
        "height": task.get("height", 0),
        "artifact_size_bytes": task.get("artifact_size_bytes"),
        "hash": task.get("hash"),
        "phash": task.get("phash"),
        "source_url": task.get("source_url"),
        "plan_id": task.get("plan_id"),
        "task_id": task.get("task_id"),
        "angle_id": task.get("angle_id"),
        "route": task.get("route"),
        "candidate_id": task.get("candidate_id"),
        "fetch_id": task.get("fetch_id"),
    }
    if task.get("local_artifact_path"):
        image_record["local_artifact_path"] = task.get("local_artifact_path")
    return {
        "id": str(task["evidence_image_id"]),
        "image_id": str(task["evidence_image_id"]),
        "observation_id": f"obs_{_sanitize_identifier(str(task['evidence_image_id']))}_{sequence:03d}",
        "evidence_image_id": str(task["evidence_image_id"]),
        "source_id": task.get("source_id"),
        "origin": task.get("origin") or "image_search",
        "image": image_record,
        "observations": observations,
        "inferences": inferences,
        "ocr_text": ocr_text,
        "ocr_outputs": [{"text": ocr_text, "provider": OPENAI_RESPONSES_VISION_PROVIDER}]
        if ocr_text
        else [],
        "visual_tasks": _string_list(task, "visual_tasks"),
        "analysis_status": "analyzed" if observations or inferences else "needs_manual_review",
        "policy_flags": _string_list(task, "policy_flags"),
        "caveats": caveats,
        "candidate_id": task["candidate_id"],
        "fetch_id": task["fetch_id"],
        "plan_id": task.get("plan_id"),
        "task_id": task["task_id"],
        "candidate_class": task.get("candidate_class"),
        "angle_id": task["angle_id"],
        "route": task.get("route"),
        "visual_provider": OPENAI_RESPONSES_VISION_PROVIDER,
        "provider": OPENAI_RESPONSES_VISION_PROVIDER,
        "provider_kind": "vlm",
        "provider_mode": provider_mode,
        "provider_run_id": provider_run_id,
        "provider_provenance": {
            "provider": OPENAI_RESPONSES_VISION_PROVIDER,
            "provider_kind": "vlm",
            "provider_mode": provider_mode,
            "provider_run_id": provider_run_id,
            "external_network_call": True,
            "external_vlm_call": True,
        },
        "model_or_tool": result.model or model,
        "observation_status": "analyzed" if observations or inferences else "needs_manual_review",
        "confidence": result.confidence,
        "policy_decision": task.get("policy_decision") or "allowed",
        "verifier_links": [],
        "report_links": [],
        "estimated_cost_usd": float(estimated_cost_usd),
        "actual_cost_usd": float(actual_cost_usd),
        "created_at": created_at,
        "visual_validation": dict(task.get("visual_validation", {}))
        if isinstance(task.get("visual_validation"), Mapping)
        else {},
        "source_url": task.get("source_url"),
        "raw_provider_metadata": raw_metadata,
        "cost_counters": {
            "estimated_cost_usd": float(estimated_cost_usd),
            "actual_cost_usd": float(actual_cost_usd),
            "usage": _redact_provider_value(
                dict(result.usage) if isinstance(result.usage, Mapping) else {},
                config=config,
            ),
        },
    }


def _codex_interactive_observation_record(
    task: Mapping[str, Any],
    *,
    result: OpenAIResponsesVisionResult,
    provider_run_id: str,
    provider_mode: str,
    model: str,
    created_at: str,
    sequence: int,
) -> dict[str, Any]:
    observations = _dedupe(
        [
            _redact_provider_text(str(item).strip(), config=None)
            for item in result.observations
            if str(item).strip()
        ]
    )
    inferences = _dedupe(
        [
            _redact_provider_text(str(item).strip(), config=None)
            for item in result.inferences
            if str(item).strip()
        ]
    )
    caveats = _dedupe(
        [
            "Codex interactive image worker output is model-generated; use observations as extracted visual evidence and treat inferences as interpretation.",
            *[
                _redact_provider_text(str(item).strip(), config=None)
                for item in result.caveats
                if str(item).strip()
            ],
        ]
    )
    ocr_text = (
        _redact_provider_text(result.ocr_text, config=None)
        if isinstance(result.ocr_text, str)
        else None
    )
    image_record = {
        "id": task["evidence_image_id"],
        "source_id": task.get("source_id"),
        "origin": task.get("origin") or "image_search",
        "image_url": task.get("image_url"),
        "page_url": task.get("page_url"),
        "local_artifact_path": task.get("local_artifact_path"),
        "mime_type": task.get("mime_type") or "image/unknown",
        "width": task.get("width", 0),
        "height": task.get("height", 0),
        "artifact_size_bytes": task.get("artifact_size_bytes"),
        "hash": task.get("hash"),
        "phash": task.get("phash"),
        "source_url": task.get("source_url"),
        "plan_id": task.get("plan_id"),
        "task_id": task.get("task_id"),
        "angle_id": task.get("angle_id"),
        "route": task.get("route"),
        "candidate_id": task.get("candidate_id"),
        "fetch_id": task.get("fetch_id"),
    }
    provider_provenance = {
        "provider": CODEX_INTERACTIVE_PROVIDER,
        "provider_kind": "vlm",
        "provider_mode": provider_mode,
        "provider_run_id": provider_run_id,
        "external_network_call": False,
        "external_vlm_call": False,
        "codex_native_handoff": True,
        "codex_interactive_handoff": True,
        "handoff_recorded": True,
        "handoff_artifact": "visual_observations.jsonl",
        "explicit_artifact_handoff": True,
        "hidden_codex_api_call": False,
    }
    return {
        "id": str(task["evidence_image_id"]),
        "image_id": str(task["evidence_image_id"]),
        "observation_id": f"obs_{_sanitize_identifier(str(task['evidence_image_id']))}_{sequence:03d}",
        "evidence_image_id": str(task["evidence_image_id"]),
        "source_id": task.get("source_id"),
        "origin": task.get("origin") or "image_search",
        "image": image_record,
        "observations": observations,
        "inferences": inferences,
        "ocr_text": ocr_text,
        "ocr_outputs": [{"text": ocr_text, "provider": CODEX_INTERACTIVE_PROVIDER}]
        if ocr_text
        else [],
        "visual_tasks": _string_list(task, "visual_tasks"),
        "analysis_status": "analyzed" if observations or inferences else "needs_manual_review",
        "policy_flags": _string_list(task, "policy_flags"),
        "caveats": caveats,
        "candidate_id": task["candidate_id"],
        "fetch_id": task["fetch_id"],
        "plan_id": task.get("plan_id"),
        "task_id": task["task_id"],
        "candidate_class": task.get("candidate_class"),
        "angle_id": task["angle_id"],
        "route": task.get("route"),
        "visual_provider": CODEX_INTERACTIVE_PROVIDER,
        "provider": CODEX_INTERACTIVE_PROVIDER,
        "provider_kind": "vlm",
        "provider_mode": provider_mode,
        "provider_run_id": provider_run_id,
        "provider_provenance": provider_provenance,
        "model_or_tool": result.model or model,
        "observation_status": "analyzed" if observations or inferences else "needs_manual_review",
        "confidence": result.confidence,
        "policy_decision": task.get("policy_decision") or "allowed",
        "verifier_links": [],
        "report_links": [],
        "estimated_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
        "created_at": created_at,
        "visual_validation": dict(task.get("visual_validation", {}))
        if isinstance(task.get("visual_validation"), Mapping)
        else {},
        "source_url": task.get("source_url"),
        "raw_provider_metadata": _redact_provider_value(
            result.raw_provider_metadata
            if isinstance(result.raw_provider_metadata, Mapping)
            else {},
            config=None,
        ),
        "cost_counters": {
            "estimated_cost_usd": 0.0,
            "actual_cost_usd": 0.0,
            "usage": _redact_provider_value(
                dict(result.usage) if isinstance(result.usage, Mapping) else {},
                config=None,
            ),
        },
        "codex_native_handoff": True,
        "codex_interactive_handoff": True,
        "handoff_recorded": True,
        "handoff_artifact": "visual_observations.jsonl",
        "explicit_artifact_handoff": True,
        "hidden_codex_api_call": False,
    }


def _reconcile_openai_fetch_lineage(
    run_dir: Path,
    observations: Sequence[Mapping[str, Any]],
) -> int:
    """Keep fetch lineage consistent after VLM analysis creates image ids."""

    if not observations:
        return 0
    fetch_path = run_dir / IMAGE_FETCH_STATUS_FILENAME
    fetches = [
        dict(item)
        for item in _read_jsonl(fetch_path)
        if isinstance(item, Mapping)
    ]
    candidates_by_id = {
        str(candidate.get("candidate_id") or candidate.get("id")): candidate
        for candidate in _read_jsonl(run_dir / "visual_candidates.jsonl")
        if isinstance(candidate, Mapping)
        and (candidate.get("candidate_id") or candidate.get("id"))
    }
    fetches_by_id = {
        str(fetch.get("fetch_id")): fetch
        for fetch in fetches
        if isinstance(fetch.get("fetch_id"), str) and fetch.get("fetch_id")
    }
    changed = 0
    for observation in observations:
        fetch_id = _first_optional_string(observation, "fetch_id")
        evidence_image_id = _first_optional_string(observation, "evidence_image_id")
        candidate_id = _first_optional_string(observation, "candidate_id")
        if not fetch_id or not evidence_image_id or not candidate_id:
            continue
        existing = fetches_by_id.get(fetch_id)
        if existing is not None:
            if not _first_optional_string(existing, "evidence_image_id"):
                existing["evidence_image_id"] = evidence_image_id
                changed += 1
            continue
        candidate = candidates_by_id.get(candidate_id)
        if not isinstance(candidate, Mapping):
            continue
        fetch = _openai_synthetic_fetch_record(
            observation,
            candidate=candidate,
            fetch_id=fetch_id,
            evidence_image_id=evidence_image_id,
            candidate_id=candidate_id,
        )
        fetches.append(fetch)
        fetches_by_id[fetch_id] = fetch
        changed += 1
    if changed:
        _write_jsonl(fetch_path, fetches)
    return changed


def _openai_synthetic_fetch_record(
    observation: Mapping[str, Any],
    *,
    candidate: Mapping[str, Any],
    fetch_id: str,
    evidence_image_id: str,
    candidate_id: str,
) -> dict[str, Any]:
    provider_provenance = candidate.get("provider_provenance")
    return {
        "fetch_id": fetch_id,
        "candidate_id": candidate_id,
        "plan_id": _first_optional_string(observation, "plan_id")
        or _first_optional_string(candidate, "plan_id"),
        "task_id": _first_optional_string(observation, "task_id")
        or _first_optional_string(candidate, "task_id"),
        "angle_id": _first_optional_string(observation, "angle_id")
        or _first_optional_string(candidate, "angle_id"),
        "route": _first_optional_string(observation, "route")
        or _first_optional_string(candidate, "route"),
        "source_search_result_id": _first_optional_string(
            candidate,
            "source_search_result_id",
        ),
        "provider": _first_optional_string(candidate, "provider") or "visual-provider",
        "provider_kind": _first_optional_string(candidate, "provider_kind") or "web_image_search",
        "provider_mode": _first_optional_string(candidate, "provider_mode") or "real",
        "provider_run_id": _first_optional_string(candidate, "provider_run_id")
        or "openai-responses-vision-url",
        "provider_provenance": dict(provider_provenance)
        if isinstance(provider_provenance, Mapping)
        else {
            "provider": _first_optional_string(candidate, "provider") or "visual-provider",
            "provider_kind": _first_optional_string(candidate, "provider_kind")
            or "web_image_search",
            "provider_mode": _first_optional_string(candidate, "provider_mode") or "real",
            "provider_run_id": _first_optional_string(candidate, "provider_run_id")
            or "openai-responses-vision-url",
        },
        "fetch_status": "fetched",
        "http_status": _first_number_value(candidate, "http_status"),
        "mime_type": _first_optional_string(observation, "mime_type")
        or _first_optional_string(candidate, "mime_type"),
        "byte_size": _first_number_value(observation, "artifact_size_bytes", "byte_size"),
        "width": _first_number_value(observation, "width"),
        "height": _first_number_value(observation, "height"),
        "hash": _first_optional_string(observation, "hash")
        or _first_optional_string(candidate, "hash"),
        "phash": _first_optional_string(observation, "phash")
        or _first_optional_string(candidate, "phash"),
        "local_artifact_path": _first_optional_string(observation, "local_artifact_path"),
        "evidence_image_id": evidence_image_id,
        "policy_decision": _first_optional_string(observation, "policy_decision")
        or _first_optional_string(candidate, "policy_decision")
        or "allowed",
        "policy_flags": _dedupe(
            _string_list(candidate, "policy_flags") + _string_list(observation, "policy_flags")
        ),
        "failure_code": None,
        "estimated_cost_usd": _first_number_value(candidate, "estimated_cost_usd") or 0.0,
        "actual_cost_usd": _first_number_value(candidate, "actual_cost_usd") or 0.0,
    }


def _first_number_value(container: Mapping[str, Any], *keys: str) -> int | float | None:
    for key in keys:
        value = container.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def _write_blocked_missing_vlm_provider(
    *,
    run_dir: Path,
    evidence: dict[str, Any],
    status_reason: str,
    configured: bool,
    available: bool,
    provider_mode: str,
    model: str,
    created_at: str,
    task_count: int,
    last_error: str | None = None,
    provider_run_id: str | None = None,
    invocations: int = 0,
    vlm_images_analyzed: int = 0,
    estimated_cost_usd: float = 0.0,
    actual_cost_usd: float = 0.0,
    external_vlm_call: bool = False,
    partial_observations_path: Path | None = None,
) -> dict[str, Any]:
    visual_observations_path = run_dir / "visual_observations.jsonl"
    _write_jsonl(visual_observations_path, [])
    stale_counts = _remove_openai_visual_evidence(evidence)
    provider_status = {
        "provider": OPENAI_RESPONSES_VISION_PROVIDER,
        "provider_kind": "vlm",
        "provider_mode": provider_mode,
        "provider_run_id": provider_run_id,
        "configured": configured,
        "available": available,
        "blocked_reason": status_reason,
        "invoked": invocations > 0,
        "invocations": invocations,
        "candidates_discovered": 0,
        "artifacts_fetched": task_count,
        "vlm_images_analyzed": vlm_images_analyzed,
        "estimated_cost_usd": float(estimated_cost_usd),
        "actual_cost_usd": float(actual_cost_usd),
        "last_error": last_error or status_reason,
        "external_network_call": external_vlm_call,
        "external_vlm_call": external_vlm_call,
        "diagnostics": {
            "model": model,
            "allow_env": OPENAI_RESPONSES_VISION_ALLOW_ENV,
            "mode_env": OPENAI_RESPONSES_VISION_MODE_ENV,
            "blocked_reason": status_reason,
            "fetched_artifacts_preserved": True,
            "stale_visual_observations_cleared": True,
            "stale_openai_images_cleared": stale_counts["images"],
            "stale_openai_visual_supports_cleared": stale_counts["visual_supports"],
        },
    }
    _write_visual_provider_status(
        run_dir=run_dir,
        provider_status=provider_status,
        status="blocked_missing_vlm_provider",
        ok=False,
        terminal=True,
        metric_classification="excluded_blocked",
        actionable_cause=f"openai-responses-vision is unavailable: {status_reason}",
        created_at=created_at,
    )
    evidence["vision_adapter"] = {
        "schema_version": VISION_ADAPTER_SCHEMA_VERSION,
        "status": "blocked_missing_vlm_provider",
        "provider": OPENAI_RESPONSES_VISION_PROVIDER,
        "provider_mode": provider_mode,
        "model_or_tool": model,
        "ingested_at": created_at,
        "visual_observations_path": "visual_observations.jsonl",
        "visual_provider_status_path": VISUAL_PROVIDER_STATUS_FILENAME,
        "images_ingested": 0,
        "vlm_images_analyzed": vlm_images_analyzed,
        "estimated_cost_usd": float(estimated_cost_usd),
        "actual_cost_usd": float(actual_cost_usd),
        "blocked_reason": status_reason,
        "external_vlm_call": external_vlm_call,
        "stale_openai_images_cleared": stale_counts["images"],
        "stale_openai_visual_supports_cleared": stale_counts["visual_supports"],
    }
    handoff = evidence.get("handoff")
    if isinstance(handoff, dict):
        handoff["visual_observations_path"] = "visual_observations.jsonl"
        handoff["visual_provider_status_path"] = VISUAL_PROVIDER_STATUS_FILENAME
        handoff["visual_status"] = "blocked_missing_vlm_provider"
    _write_json(run_dir / "evidence.json", evidence)
    validation = validate_artifacts(
        evidence_path=run_dir / "evidence.json",
        visual_observations_path=visual_observations_path,
    )
    visual_artifact_validation = validate_visual_artifacts(run_dir=run_dir, evidence_path=None)
    status = _base_status(run_dir, evidence, "blocked_missing_vlm_provider")
    status.update(
        {
            "ok": False,
            "terminal": True,
            "provider": OPENAI_RESPONSES_VISION_PROVIDER,
            "provider_mode": provider_mode,
            "model_or_tool": model,
            "blocked_reason": status_reason,
            "images_ingested": 0,
            "vlm_images_analyzed": vlm_images_analyzed,
            "estimated_cost_usd": float(estimated_cost_usd),
            "actual_cost_usd": float(actual_cost_usd),
            "validation": validation.to_dict(),
            "visual_artifact_validation": visual_artifact_validation.to_dict(),
            "external_vlm_call": external_vlm_call,
            "stale_openai_images_cleared": stale_counts["images"],
            "stale_openai_visual_supports_cleared": stale_counts["visual_supports"],
            "artifacts": {
                "evidence": str(run_dir / "evidence.json"),
                "visual_observations": str(visual_observations_path),
                "visual_provider_status": str(run_dir / VISUAL_PROVIDER_STATUS_FILENAME),
                "vision_ingest_status": str(run_dir / "vision_ingest_status.json"),
            },
        }
    )
    if partial_observations_path is not None:
        status["artifacts"]["partial_openai_responses_vision_observations"] = str(
            partial_observations_path
        )
        evidence["vision_adapter"]["partial_observations_path"] = _relative_or_string(
            run_dir,
            partial_observations_path,
        )
        _write_json(run_dir / "evidence.json", evidence)
    record_stage_trace(
        run_dir,
        stage="ingest_vision",
        agent_role="vision_adapter",
        status_payload=status,
        prompt_summary="Analyze fetched visual artifacts with openai-responses-vision.",
        tool_call_summary="Blocked before VLM call; fetched visual artifacts and lineage files were preserved.",
    )
    _write_json(run_dir / "vision_ingest_status.json", status)
    return status


def _write_blocked_missing_codex_vlm_provider(
    *,
    run_dir: Path,
    evidence: dict[str, Any],
    status_reason: str,
    configured: bool,
    available: bool,
    provider_mode: str,
    model: str,
    created_at: str,
    task_count: int,
    last_error: str | None = None,
    provider_run_id: str | None = None,
    invocations: int = 0,
    vlm_images_analyzed: int = 0,
    partial_observations_path: Path | None = None,
) -> dict[str, Any]:
    visual_observations_path = run_dir / "visual_observations.jsonl"
    _write_jsonl(visual_observations_path, [])
    stale_counts = _remove_provider_visual_evidence(evidence, CODEX_INTERACTIVE_PROVIDER)
    provider_status = {
        "provider": CODEX_INTERACTIVE_PROVIDER,
        "provider_kind": "vlm",
        "provider_mode": provider_mode,
        "provider_run_id": provider_run_id,
        "configured": configured,
        "available": available,
        "blocked_reason": status_reason,
        "invoked": invocations > 0,
        "invocations": invocations,
        "candidates_discovered": 0,
        "artifacts_fetched": task_count,
        "vlm_images_analyzed": vlm_images_analyzed,
        "estimated_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
        "last_error": last_error or status_reason,
        "external_network_call": False,
        "external_vlm_call": False,
        "codex_native_handoff": True,
        "codex_interactive_handoff": True,
        "handoff_recorded": True,
        "handoff_artifact": "visual_observations.jsonl",
        "explicit_artifact_handoff": True,
        "hidden_codex_api_call": False,
        "diagnostics": {
            "model": model,
            "worker_command": "codex exec --json --image <artifact>",
            "blocked_reason": status_reason,
            "fetched_artifacts_preserved": True,
            "stale_visual_observations_cleared": True,
            "stale_codex_images_cleared": stale_counts["images"],
            "stale_codex_visual_supports_cleared": stale_counts["visual_supports"],
        },
    }
    _write_visual_provider_status(
        run_dir=run_dir,
        provider_status=provider_status,
        status="blocked_missing_vlm_provider",
        ok=False,
        terminal=True,
        metric_classification="excluded_blocked",
        actionable_cause=f"codex-interactive visual worker is unavailable: {status_reason}",
        created_at=created_at,
    )
    evidence["vision_adapter"] = {
        "schema_version": VISION_ADAPTER_SCHEMA_VERSION,
        "status": "blocked_missing_vlm_provider",
        "provider": CODEX_INTERACTIVE_PROVIDER,
        "provider_mode": provider_mode,
        "model_or_tool": model,
        "ingested_at": created_at,
        "visual_observations_path": "visual_observations.jsonl",
        "visual_provider_status_path": VISUAL_PROVIDER_STATUS_FILENAME,
        "images_ingested": 0,
        "vlm_images_analyzed": vlm_images_analyzed,
        "estimated_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
        "blocked_reason": status_reason,
        "external_vlm_call": False,
        "stale_codex_images_cleared": stale_counts["images"],
        "stale_codex_visual_supports_cleared": stale_counts["visual_supports"],
    }
    handoff = evidence.get("handoff")
    if isinstance(handoff, dict):
        handoff["visual_observations_path"] = "visual_observations.jsonl"
        handoff["visual_provider_status_path"] = VISUAL_PROVIDER_STATUS_FILENAME
        handoff["visual_status"] = "blocked_missing_vlm_provider"
    _write_json(run_dir / "evidence.json", evidence)
    validation = validate_artifacts(
        evidence_path=run_dir / "evidence.json",
        visual_observations_path=visual_observations_path,
    )
    visual_artifact_validation = validate_visual_artifacts(run_dir=run_dir, evidence_path=None)
    status = _base_status(run_dir, evidence, "blocked_missing_vlm_provider")
    status.update(
        {
            "ok": False,
            "terminal": True,
            "provider": CODEX_INTERACTIVE_PROVIDER,
            "provider_mode": provider_mode,
            "model_or_tool": model,
            "blocked_reason": status_reason,
            "images_ingested": 0,
            "vlm_images_analyzed": vlm_images_analyzed,
            "estimated_cost_usd": 0.0,
            "actual_cost_usd": 0.0,
            "validation": validation.to_dict(),
            "visual_artifact_validation": visual_artifact_validation.to_dict(),
            "external_vlm_call": False,
            "stale_codex_images_cleared": stale_counts["images"],
            "stale_codex_visual_supports_cleared": stale_counts["visual_supports"],
            "artifacts": {
                "evidence": str(run_dir / "evidence.json"),
                "visual_observations": str(visual_observations_path),
                "visual_provider_status": str(run_dir / VISUAL_PROVIDER_STATUS_FILENAME),
                "vision_ingest_status": str(run_dir / "vision_ingest_status.json"),
            },
        }
    )
    if partial_observations_path is not None:
        status["artifacts"]["partial_codex_interactive_observations"] = str(
            partial_observations_path
        )
        evidence["vision_adapter"]["partial_observations_path"] = _relative_or_string(
            run_dir,
            partial_observations_path,
        )
        _write_json(run_dir / "evidence.json", evidence)
    record_stage_trace(
        run_dir,
        stage="ingest_vision",
        agent_role="vision_adapter",
        status_payload=status,
        prompt_summary="Analyze fetched visual artifacts with codex-interactive.",
        tool_call_summary="Blocked before Codex image worker completion; fetched visual artifacts and lineage files were preserved.",
    )
    _write_json(run_dir / "vision_ingest_status.json", status)
    return status


def _write_visual_provider_status(
    *,
    run_dir: Path,
    provider_status: Mapping[str, Any],
    status: str,
    ok: bool,
    terminal: bool,
    metric_classification: str,
    actionable_cause: str,
    created_at: str,
) -> None:
    envelope = (
        automatic_visual_status_envelope(status)
        if status in {"blocked_missing_vlm_provider"}
        else {"ok": ok, "terminal": terminal, "metric_classification": metric_classification}
    )
    provider_name = _first_optional_string(provider_status, "provider")
    providers = _existing_visual_provider_records(run_dir)
    providers = [
        provider
        for provider in providers
        if provider.get("provider") != provider_name
    ]
    providers.append(dict(provider_status))
    payload = {
        "schema_version": VISUAL_PROVIDER_STATUS_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "run_dir": run_dir.name,
        "status": status,
        "ok": envelope["ok"],
        "terminal": envelope["terminal"],
        "created_at": created_at,
        "metric_classification": envelope["metric_classification"],
        "minimums": visual_minimums_for_run(run_dir),
        "providers": providers,
        "diagnostics": {"actionable_cause": actionable_cause},
        "artifacts": {
            "visual_candidates": "visual_candidates.jsonl",
            "image_fetch_status": IMAGE_FETCH_STATUS_FILENAME,
            "visual_observations": "visual_observations.jsonl",
            "visual_provider_status": VISUAL_PROVIDER_STATUS_FILENAME,
        },
    }
    _write_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME, _redact_provider_value(payload, config=None))


def _has_openai_vision_handoff(run_dir: Path) -> bool:
    candidates_path = run_dir / "visual_candidates.jsonl"
    fetch_path = run_dir / IMAGE_FETCH_STATUS_FILENAME
    return any(
        isinstance(item, Mapping)
        for path in (candidates_path, fetch_path)
        for item in _read_jsonl(path)
    )


def _read_jsonl(path: Path) -> list[Any]:
    if not path.exists():
        return []
    records: list[Any] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise VisionAdapterError(f"invalid JSONL record in {path} line {line_number}: {exc}") from exc
    return records


def _existing_visual_provider_records(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / VISUAL_PROVIDER_STATUS_FILENAME
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, Mapping) or not isinstance(payload.get("providers"), list):
        return []
    return [dict(item) for item in payload["providers"] if isinstance(item, Mapping)]


def _config_string(
    overrides: Mapping[str, Any] | None,
    key: str,
    *,
    env_names: Sequence[str] = (),
    default: str | None = None,
) -> str | None:
    if overrides is not None and key in overrides:
        value = overrides.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None
    for env_name in env_names:
        value = os.getenv(env_name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _config_bool(
    overrides: Mapping[str, Any] | None,
    key: str,
    *,
    env_names: Sequence[str] = (),
    default: bool,
) -> bool:
    if overrides is not None and key in overrides:
        value = overrides.get(key)
        if isinstance(value, bool):
            return value
        if value is not None:
            return str(value).strip().lower() in TRUE_CONFIG_VALUES
    for env_name in env_names:
        value = os.getenv(env_name)
        if value is not None and value.strip():
            return value.strip().lower() in TRUE_CONFIG_VALUES
    return default


def _config_int(
    overrides: Mapping[str, Any] | None,
    key: str,
    *,
    env_names: Sequence[str] = (),
    default: int | None,
) -> int | None:
    if overrides is not None and key in overrides:
        return _parse_optional_int(overrides.get(key), default=default)
    for env_name in env_names:
        value = os.getenv(env_name)
        if value is not None and value.strip():
            return _parse_optional_int(value, default=default)
    return default


def _config_float(
    overrides: Mapping[str, Any] | None,
    key: str,
    *,
    env_names: Sequence[str] = (),
    default: float,
) -> float:
    if overrides is not None and key in overrides:
        return _parse_float(overrides.get(key), default=default)
    for env_name in env_names:
        value = os.getenv(env_name)
        if value is not None and value.strip():
            return _parse_float(value, default=default)
    return default


def _parse_optional_int(value: Any, *, default: int | None) -> int | None:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if value > 0 else default
    if isinstance(value, float) and value.is_integer():
        integer = int(value)
        return integer if integer > 0 else default
    if isinstance(value, str) and value.strip():
        try:
            integer = int(float(value))
        except ValueError:
            return default
        return integer if integer > 0 else default
    return default


def _parse_float(value: Any, *, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _task_number(
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any] | None,
    *keys: str,
) -> int | float:
    for key in keys:
        for container in (primary, secondary or {}):
            value = container.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return value
            if isinstance(value, str) and value.strip():
                try:
                    numeric = float(value)
                except ValueError:
                    continue
                return int(numeric) if numeric.is_integer() else numeric
    return 0


def _estimated_cost_for_task(
    task: Mapping[str, Any],
    *,
    config: OpenAIResponsesVisionConfig,
) -> float:
    if config.estimated_cost_usd_per_image > 0:
        return config.estimated_cost_usd_per_image
    width = _numeric_or_zero(task.get("width"))
    height = _numeric_or_zero(task.get("height"))
    if width <= 0 or height <= 0:
        return 0.0
    patch_count = int(((width + 31) // 32) * ((height + 31) // 32))
    estimated_tokens = min(max(patch_count, 1), 1536)
    return round(estimated_tokens * 0.00000001, 8)


def _numeric_or_zero(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    if isinstance(value, str) and value.strip():
        try:
            return max(int(float(value)), 0)
        except ValueError:
            return 0
    return 0


def _source_id_for_task(evidence: Mapping[str, Any]) -> str | None:
    sources = evidence.get("sources")
    if not isinstance(sources, list):
        return None
    ids = [
        source.get("id")
        for source in sources
        if isinstance(source, Mapping) and isinstance(source.get("id"), str)
    ]
    return ids[0] if len(ids) == 1 else None


def _source_url_for_task(evidence: Mapping[str, Any], source_id: Any) -> str | None:
    if not isinstance(source_id, str) or not source_id:
        return None
    sources = evidence.get("sources")
    if not isinstance(sources, list):
        return None
    for source in sources:
        if isinstance(source, Mapping) and source.get("id") == source_id:
            return _first_optional_string(source, "url")
    return None


def _fetch_id_for_candidate_id(candidate_id: str) -> str:
    return "fetch_" + _sanitize_identifier(candidate_id.removeprefix("cand_"))


def _image_id_for_candidate_id(candidate_id: str) -> str:
    return "img_" + _sanitize_identifier(candidate_id.removeprefix("cand_"))


def _plan_id_for_visual_task(*, task_id: str, angle_id: str, route: str) -> str:
    return "plan_" + _sanitize_identifier(f"{task_id}_{angle_id}_{route}")


def _redact_provider_text(
    value: str,
    *,
    config: OpenAIResponsesVisionConfig | None,
) -> str:
    redacted = value
    if config is not None and config.api_key:
        redacted = redacted.replace(config.api_key, "[REDACTED]")
    parts: list[str] = []
    cursor = 0
    for match in URL_PATTERN.finditer(redacted):
        parts.append(_redact_secret_fields(redacted[cursor : match.start()]))
        parts.append(_sanitize_provider_url(match.group(0)))
        cursor = match.end()
    parts.append(_redact_secret_fields(redacted[cursor:]))
    return "".join(parts)


def _preview_text(value: str | None, *, limit: int = 500) -> str:
    text = (value or "").strip()
    if not text:
        return "<empty>"
    return _redact_provider_text(text[:limit], config=None)


def _redact_secret_fields(value: str) -> str:
    redacted = BEARER_VALUE_PATTERN.sub(r"\1 [REDACTED]", value)
    redacted = PRIVATE_ABSOLUTE_PATH_PATTERN.sub("[REDACTED_PATH]", redacted)
    redacted = SECRET_JSON_FIELD_PATTERN.sub(r"\1[REDACTED]\3", redacted)
    return SECRET_FIELD_PATTERN.sub(r"\1\2[REDACTED]", redacted)


def _is_sensitive_provider_metadata_key(key: str) -> bool:
    return bool(SENSITIVE_PROVIDER_KEY_PATTERN.search(key))


def _sanitize_provider_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value

    hostname = parsed.hostname
    if not hostname:
        return value
    host = hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host

    query_items = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        query_items.append((key, "[REDACTED]" if _is_sensitive_url_query_key(key) else item))
    query = urlencode(query_items, doseq=True, safe="[]") if query_items else ""
    fragment = "[REDACTED]" if parsed.fragment else ""
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, fragment))


def _is_sensitive_url_query_key(key: str) -> bool:
    normalized = key.strip().lower()
    if normalized in SENSITIVE_URL_QUERY_KEYS:
        return True
    compact = normalized.replace("-", "_")
    if compact in SENSITIVE_URL_QUERY_KEYS:
        return True
    return bool(
        re.search(
            r"(^|[_-])(api[_-]?key|access[_-]?token|refresh[_-]?token|session[_-]?token|"
            r"client[_-]?secret|token|secret|credential|credentials|password|auth|authorization|"
            r"bearer)([_-]|$)",
            normalized,
        )
    )


def _redact_provider_value(
    value: Any,
    *,
    config: OpenAIResponsesVisionConfig | None,
) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_provider_metadata_key(key_text):
                redacted[key_text] = "[REDACTED]"
            else:
                redacted[key_text] = _redact_provider_value(item, config=config)
        return redacted
    if isinstance(value, list):
        return [_redact_provider_value(item, config=config) for item in value]
    if isinstance(value, tuple):
        return [_redact_provider_value(item, config=config) for item in value]
    if isinstance(value, str):
        return _redact_provider_text(value, config=config)
    return value


def _remove_openai_visual_evidence(evidence: dict[str, Any]) -> dict[str, int]:
    return _remove_provider_visual_evidence(evidence, OPENAI_RESPONSES_VISION_PROVIDER)


def _remove_provider_visual_evidence(
    evidence: dict[str, Any],
    provider: str,
) -> dict[str, int]:
    images = evidence.get("images", [])
    if not isinstance(images, list):
        return {"images": 0, "visual_supports": 0, "supporting_images": 0}

    kept_images: list[Any] = []
    removed_image_ids: set[str] = set()
    removed_images = 0
    for image in images:
        if isinstance(image, Mapping) and _is_provider_visual_image(image, provider):
            removed_images += 1
            image_id = image.get("id")
            if isinstance(image_id, str) and image_id:
                removed_image_ids.add(image_id)
            continue
        kept_images.append(image)

    if not removed_images:
        return {"images": 0, "visual_supports": 0, "supporting_images": 0}

    evidence["images"] = kept_images
    removed_supports = 0
    removed_supporting_images = 0
    claims = evidence.get("claims", [])
    if isinstance(claims, list):
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            supporting_images = claim.get("supporting_images")
            if isinstance(supporting_images, list):
                filtered_supporting_images = [
                    item
                    for item in supporting_images
                    if not (isinstance(item, str) and item in removed_image_ids)
                ]
                removed_supporting_images += len(supporting_images) - len(filtered_supporting_images)
                claim["supporting_images"] = filtered_supporting_images
            visual_supports = claim.get("visual_supports")
            if isinstance(visual_supports, list):
                filtered_supports = [
                    support
                    for support in visual_supports
                    if not (
                        isinstance(support, Mapping)
                        and isinstance(support.get("image_id"), str)
                        and support.get("image_id") in removed_image_ids
                    )
                ]
                removed_supports += len(visual_supports) - len(filtered_supports)
                claim["visual_supports"] = filtered_supports

    return {
        "images": removed_images,
        "visual_supports": removed_supports,
        "supporting_images": removed_supporting_images,
    }


def _is_openai_visual_image(image: Mapping[str, Any]) -> bool:
    return _is_provider_visual_image(image, OPENAI_RESPONSES_VISION_PROVIDER)


def _is_provider_visual_image(image: Mapping[str, Any], provider: str) -> bool:
    for key in ("analysis_provider", "provider", "visual_provider"):
        if _first_optional_string(image, key) == provider:
            return True
    provenance = image.get("provider_provenance")
    if isinstance(provenance, Mapping):
        return _first_optional_string(provenance, "provider") == provider
    return False


def _visual_claims_from_observations(
    evidence: Mapping[str, Any],
    *,
    now: str,
) -> list[dict[str, Any]]:
    images = evidence.get("images", [])
    claims = evidence.get("claims", [])
    if not isinstance(images, list) or not isinstance(claims, list):
        return []

    sources_by_id = _sources_by_id(evidence.get("sources", []))
    used_claim_ids = _existing_ids(claims)
    existing_generated_keys = _existing_observation_claim_keys(claims)
    generated: list[dict[str, Any]] = []
    for image in images:
        if not isinstance(image, Mapping):
            continue
        image_id = image.get("id")
        source_id = image.get("source_id")
        if not isinstance(image_id, str) or not isinstance(source_id, str):
            continue
        if not _image_can_generate_visual_claim(image, source=sources_by_id.get(source_id)):
            continue
        observation_index, observation_text = _first_observation(image)
        if observation_index is None or observation_text is None:
            continue
        claim_key = (image_id, observation_index, observation_text)
        if claim_key in existing_generated_keys:
            continue

        claim_id = _unique_id(
            f"claim_visual_{image_id}_{observation_index + 1}",
            used_claim_ids,
        )
        support = _visual_support_from_image(
            image,
            observation_index=observation_index,
            observation_text=observation_text,
        )
        generated.append(
            {
                "id": claim_id,
                "text": _visual_claim_text(observation_text),
                "claim_type": "visual",
                "supporting_sources": [source_id],
                "supporting_images": [image_id],
                "visual_supports": [support],
                "quote_spans": [],
                "votes": [],
                "verification_status": "unverified",
                "review_status": "not_reviewed",
                "promotion_status": "not_eligible",
                "confidence": "low",
                "caveats": _visual_claim_caveats(image),
                "angle_id": _first_optional_string(image, "angle_id"),
                "route": _first_optional_string(image, "route") or "visual_required",
                "visual_tasks": _string_list(image, "visual_tasks"),
                "created_at": now,
                "extraction_stage": "vision_adapter_observation_claim",
                "source_image_id": image_id,
                "source_observation_ref": support["observation_ref"],
            }
        )
        existing_generated_keys.add(claim_key)
    return generated


def _existing_observation_claim_keys(claims: Sequence[Any]) -> set[tuple[str, int, str]]:
    keys: set[tuple[str, int, str]] = set()
    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        raw_supports = claim.get("visual_supports", [])
        if not isinstance(raw_supports, list):
            continue
        for support in raw_supports:
            if not isinstance(support, Mapping):
                continue
            image_id = support.get("image_id")
            observation_index = support.get("observation_index")
            observation_text = support.get("observation_text")
            if (
                isinstance(image_id, str)
                and isinstance(observation_index, int)
                and isinstance(observation_text, str)
            ):
                keys.add((image_id, observation_index, observation_text))
    return keys


def _image_can_generate_visual_claim(
    image: Mapping[str, Any],
    *,
    source: Mapping[str, Any] | None,
) -> bool:
    if image.get("analysis_status") != "analyzed":
        return False
    if _policy_record_blocks(image) or _policy_record_blocks(source):
        return False
    if _string_list(image, "policy_flags"):
        return False
    observations = _string_list(image, "observations")
    return bool(observations)


def _policy_record_blocks(record: Mapping[str, Any] | None) -> bool:
    if not isinstance(record, Mapping):
        return False
    if record.get("policy_decision") in {
        "blocked",
        "budget_pruned",
        "disallowed",
        "manual_review",
        "restricted",
    }:
        return True
    if record.get("license_policy") in {"restricted", "manual_review"}:
        return True
    if record.get("robots_policy") in {"disallowed", "manual_review"}:
        return True
    return False


def _visual_support_from_image(
    image: Mapping[str, Any],
    *,
    observation_index: int,
    observation_text: str,
) -> dict[str, Any]:
    image_id = str(image["id"])
    support = {
        "image_id": image_id,
        "observation_ref": f"images.{image_id}.observations[{observation_index}]",
        "observation_index": observation_index,
        "observation_text": observation_text,
        "relation_type": _visual_relation_type(image),
        "provider": _visual_support_provider(image),
        "rationale": "Generated from an explicit VLM observation, not an inference-only note.",
        "confidence": 0.74,
    }
    support.update(_visual_lineage_from_image(image))
    return support


def _visual_lineage_from_image(image: Mapping[str, Any]) -> dict[str, str]:
    lineage: dict[str, str] = {}
    for field in (
        "plan_id",
        "task_id",
        "angle_id",
        "route",
        "candidate_id",
        "fetch_id",
    ):
        value = _first_optional_string(image, field)
        if value:
            lineage[field] = value
    evidence_image_id = _first_optional_string(
        image,
        "evidence_image_id",
    ) or _first_optional_string(image, "id")
    if evidence_image_id:
        lineage["evidence_image_id"] = evidence_image_id
    return lineage


def _visual_claim_text(observation_text: str) -> str:
    stripped = " ".join(observation_text.strip().split())
    stripped = stripped.rstrip(".")
    return f"Visual observation: {stripped}."


def _visual_claim_caveats(image: Mapping[str, Any]) -> list[str]:
    caveats = _string_list(image, "caveats")
    if _string_list(image, "inferences"):
        caveats.append("VLM inferences are preserved separately and were not used as claim text.")
    return _dedupe(caveats)


def _link_visual_evidence_to_claims(evidence: dict[str, Any]) -> int:
    claims = evidence.get("claims", [])
    images = evidence.get("images", [])
    if not isinstance(claims, list) or not isinstance(images, list):
        return 0

    routing_by_angle = _routing_by_angle(evidence.get("routing", []))
    sources_by_id = _sources_by_id(evidence.get("sources", []))
    created = 0
    for claim in claims:
        if not isinstance(claim, dict) or not _claim_accepts_visual_link(
            claim,
            routing_by_angle=routing_by_angle,
            sources_by_id=sources_by_id,
        ):
            continue

        supports = _valid_existing_visual_supports(claim, images)
        support_keys = {
            (support["image_id"], support["observation_index"])
            for support in supports
        }
        supporting_images = _dedupe(_string_list(claim, "supporting_images"))
        for image in images:
            if not isinstance(image, Mapping):
                continue
            observation_index, observation_text = _first_observation(image)
            if observation_index is None or observation_text is None:
                continue
            if not _image_matches_claim(
                claim,
                image,
                routing_by_angle=routing_by_angle,
                sources_by_id=sources_by_id,
            ):
                continue

            image_id = str(image["id"])
            key = (image_id, observation_index)
            if key not in support_keys:
                support = _visual_support_from_image(
                    image,
                    observation_index=observation_index,
                    observation_text=observation_text,
                )
                support["rationale"] = _visual_support_rationale(claim, image)
                supports.append(support)
                support_keys.add(key)
                created += 1
            if image_id not in supporting_images:
                supporting_images.append(image_id)
        if supports:
            claim["supporting_images"] = supporting_images
            claim["visual_supports"] = supports
    return created


def _valid_existing_visual_supports(
    claim: Mapping[str, Any],
    images: Sequence[Any],
) -> list[dict[str, Any]]:
    images_by_id = {
        image["id"]: image
        for image in images
        if isinstance(image, Mapping) and isinstance(image.get("id"), str)
    }
    supports: list[dict[str, Any]] = []
    raw_supports = claim.get("visual_supports", [])
    if not isinstance(raw_supports, list):
        return supports
    for raw_support in raw_supports:
        if not isinstance(raw_support, Mapping):
            continue
        image_id = raw_support.get("image_id")
        observation_index = raw_support.get("observation_index")
        if not isinstance(image_id, str) or not isinstance(observation_index, int):
            continue
        image = images_by_id.get(image_id)
        if not isinstance(image, Mapping):
            continue
        observations = image.get("observations", [])
        if (
            not isinstance(observations, list)
            or observation_index < 0
            or observation_index >= len(observations)
        ):
            continue
        observation_text = observations[observation_index]
        if not isinstance(observation_text, str) or not observation_text:
            continue
        support = dict(raw_support)
        support["observation_ref"] = f"images.{image_id}.observations[{observation_index}]"
        support["observation_text"] = observation_text
        for field, value in _visual_lineage_from_image(image).items():
            support.setdefault(field, value)
        supports.append(support)
    return supports


def _claim_accepts_visual_link(
    claim: Mapping[str, Any],
    *,
    routing_by_angle: Mapping[str, str],
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    if claim.get("claim_type") in {"visual", "mixed"}:
        return True
    route = _claim_route(
        claim,
        routing_by_angle=routing_by_angle,
        sources_by_id=sources_by_id,
    )
    return route == "visual_required"


def _image_matches_claim(
    claim: Mapping[str, Any],
    image: Mapping[str, Any],
    *,
    routing_by_angle: Mapping[str, str],
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    if image.get("analysis_status") != "analyzed":
        return False
    if not isinstance(image.get("id"), str):
        return False
    if _policy_record_blocks(image):
        return False
    if _string_list(image, "policy_flags"):
        return False
    if claim.get("extraction_stage") == "vision_adapter_observation_claim":
        return image.get("id") == claim.get("source_image_id")

    source_id = image.get("source_id")
    if isinstance(source_id, str) and source_id in _string_list(claim, "supporting_sources"):
        return True
    claim_angle = claim.get("angle_id")
    image_angle = image.get("angle_id")
    if isinstance(claim_angle, str) and claim_angle and claim_angle == image_angle:
        return True
    claim_route = _claim_route(
        claim,
        routing_by_angle=routing_by_angle,
        sources_by_id=sources_by_id,
    )
    return claim_route == "visual_required" and image.get("route") == "visual_required"


def _claim_route(
    claim: Mapping[str, Any],
    *,
    routing_by_angle: Mapping[str, str],
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    for key in ("route", "search_route", "verification_route"):
        value = claim.get(key)
        if value in {"text_only", "visual_required", "visual_optional"}:
            return str(value)
    angle_id = claim.get("angle_id")
    if isinstance(angle_id, str) and angle_id in routing_by_angle:
        return routing_by_angle[angle_id]
    source_routes = {
        source.get("route")
        for source_id in _string_list(claim, "supporting_sources")
        for source in [sources_by_id.get(source_id)]
        if isinstance(source, Mapping)
        and source.get("route") in {"text_only", "visual_required", "visual_optional"}
    }
    if "visual_required" in source_routes:
        return "visual_required"
    if len(source_routes) == 1:
        return str(next(iter(source_routes)))
    return "text_only"


def _routing_by_angle(routes: Any) -> dict[str, str]:
    if not isinstance(routes, list):
        return {}
    return {
        route["id"]: route["modality"]
        for route in routes
        if isinstance(route, Mapping)
        and isinstance(route.get("id"), str)
        and route.get("modality") in {"text_only", "visual_required", "visual_optional"}
    }


def _first_observation(image: Mapping[str, Any]) -> tuple[int | None, str | None]:
    observations = image.get("observations", [])
    if not isinstance(observations, list):
        return None, None
    for index, observation in enumerate(observations):
        if isinstance(observation, str) and observation.strip():
            return index, observation.strip()
    return None, None


def _visual_relation_type(image: Mapping[str, Any]) -> str:
    tasks = set(_string_list(image, "visual_tasks"))
    if "chart_reading" in tasks or "chart_support" in tasks:
        return "chart_support"
    if "ocr" in tasks and image.get("ocr_text"):
        return "ocr_support"
    if image.get("origin") == "screenshot":
        return "screenshot_support"
    return "visual_match"


def _visual_support_provider(image: Mapping[str, Any]) -> str:
    return (
        _first_optional_string(image, "visual_provider")
        or _first_optional_string(image, "analysis_provider")
        or "unknown"
    )


def _visual_support_rationale(claim: Mapping[str, Any], image: Mapping[str, Any]) -> str:
    source_id = image.get("source_id")
    if isinstance(source_id, str) and source_id in _string_list(claim, "supporting_sources"):
        return f"Linked because claim and image cite source_id '{source_id}'."
    claim_angle = claim.get("angle_id")
    if isinstance(claim_angle, str) and claim_angle == image.get("angle_id"):
        return f"Linked because claim and image share angle_id '{claim_angle}'."
    return "Linked because both claim and image belong to a visual-required route."


def _normalize_visual_record(
    record: Mapping[str, Any],
    *,
    provider: str,
    index: int,
    run_dir: Path,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    existing_image_ids: set[str],
    used_image_ids: set[str],
    now: str,
) -> dict[str, Any]:
    image_record = record.get("image")
    image = image_record if isinstance(image_record, Mapping) else {}
    raw_image_id = _first_string(record, "id", "image_id") or _first_string(
        image,
        "id",
        "image_id",
    )
    image_id = _image_id(
        raw_image_id,
        index=index,
        existing_image_ids=existing_image_ids,
        used_image_ids=used_image_ids,
    )
    source_id = _first_string(record, "source_id") or _first_string(image, "source_id")
    source_ids = set(sources_by_id)
    if source_id is None and len(source_ids) == 1:
        source_id = next(iter(source_ids))
    if source_id is None:
        raise VisionAdapterError("visual observation requires source_id")
    if source_ids and source_id not in source_ids:
        raise VisionAdapterError(f"source_id '{source_id}' does not exist in evidence.sources")
    source = sources_by_id.get(source_id)

    explicit_artifact_path = _first_string(
        record,
        "local_artifact_path",
        "artifact_path",
        "image_path",
    ) or _first_string(image, "local_artifact_path", "artifact_path", "path")
    artifact_path = explicit_artifact_path or f"images/{image_id}.json"
    artifact_file = _resolve_run_relative_path(run_dir, artifact_path)
    local_path = artifact_file.relative_to(run_dir.resolve()).as_posix()
    raw_image_url = _first_optional_string(record, "image_url") or _first_optional_string(
        image,
        "image_url",
        "url",
    )
    raw_page_url = _first_optional_string(record, "page_url", "source_url") or _first_optional_string(
        image,
        "page_url",
        "source_url",
    )
    image_url = _validated_optional_url(raw_image_url, "image_url", allow_file=True)
    page_url = _validated_optional_url(raw_page_url, "page_url", allow_file=False)
    if explicit_artifact_path is not None and not artifact_file.is_file():
        raise VisionAdapterError(
            f"local_artifact_path '{local_path}' does not reference an existing run-local file"
        )
    if image_url is not None and image_url.startswith("file://") and not _file_uri_exists(image_url):
        raise VisionAdapterError(f"image_url '{image_url}' does not reference an existing file")
    if artifact_file.is_file():
        fallback_image_url = artifact_file.as_uri()
    else:
        fallback_image_url = None
    image_url = image_url or fallback_image_url
    if image_url is None and page_url is None:
        raise VisionAdapterError(
            "visual observation requires an existing local_artifact_path, "
            "valid image_url, or valid page_url"
        )

    observations = _string_list(
        record,
        "observations",
        "visual_observations",
        "human_observations",
    )
    inferences = _string_list(record, "inferences", "visual_inferences")
    response = record.get("response")
    if isinstance(response, Mapping):
        observations.extend(_string_list(response, "observations", "visual_observations"))
        inferences.extend(_string_list(response, "inferences", "visual_inferences"))
        output_text = _first_optional_string(response, "output_text", "text")
        if output_text:
            observations.append(output_text)
    elif isinstance(response, str) and response.strip():
        observations.append(response.strip())

    ocr_text = (
        _first_optional_string(record, "ocr_text", "text_in_image")
        or _first_optional_string(image, "ocr_text", "text_in_image")
    )
    if ocr_text:
        ocr_text = _redact_provider_text(ocr_text, config=None)
    ocr_outputs = _ocr_outputs(record, image, ocr_text=ocr_text)
    ocr_outputs = _redact_provider_value(ocr_outputs, config=None)
    visual_summary = (
        _first_optional_string(record, "vlm_visual_summary", "visual_summary")
        or _first_optional_string(image, "vlm_visual_summary", "visual_summary")
    )
    if visual_summary:
        visual_summary = _redact_provider_text(visual_summary, config=None)
    visual_description = (
        _first_optional_string(record, "vlm_visual_description", "visual_description")
        or _first_optional_string(image, "vlm_visual_description", "visual_description")
    )
    if visual_description:
        visual_description = _redact_provider_text(visual_description, config=None)
    if visual_summary:
        observations.append(visual_summary)
    if visual_description and visual_description != visual_summary:
        observations.append(visual_description)

    observations = _dedupe(_redact_provider_text(item, config=None) for item in observations)
    inferences = _dedupe(_redact_provider_text(item, config=None) for item in inferences)
    caveats = _dedupe(
        _redact_provider_text(item, config=None)
        for item in (_string_list(record, "caveats") + _string_list(image, "caveats"))
    )
    if explicit_artifact_path is None and not artifact_file.exists():
        caveats.append("metadata-only visual record; no local image artifact was provided")

    artifact_size_bytes = _artifact_size_bytes(record, image, artifact_file)
    visual = {
        "id": image_id,
        "source_id": source_id,
        "origin": _origin(record, image, provider),
        "source_url": _first_optional_string(source or {}, "url"),
        "image_url": image_url,
        "page_url": page_url,
        "local_artifact_path": local_path,
        "mime_type": _mime_type(record, image, local_path),
        "artifact_size_bytes": artifact_size_bytes,
        "width": _number(record, image, "width"),
        "height": _number(record, image, "height"),
        "hash": _hash(record, image, artifact_file),
        "phash": _first_optional_string(record, "phash") or _first_optional_string(image, "phash"),
        "ocr_text": ocr_text,
        "ocr_outputs": ocr_outputs,
        "observations": observations,
        "inferences": inferences,
        "vlm_visual_summary": visual_summary,
        "vlm_visual_description": visual_description,
        "visual_tasks": _dedupe(_string_list(record, "visual_tasks", "tasks")),
        "analysis_provider": provider,
        "analysis_status": _analysis_status(record, observations, inferences),
        "policy_flags": _dedupe(_string_list(record, "policy_flags")),
        "caveats": caveats,
        "analyzed_at": _first_optional_string(record, "analyzed_at") or now,
        "adapter_input_id": _first_optional_string(record, "adapter_input_id", "id", "image_id"),
        "vision_adapter_stage": VISION_ADAPTER_STAGE,
    }
    raw_provider_metadata = record.get("raw_provider_metadata")
    if isinstance(raw_provider_metadata, Mapping):
        visual["raw_provider_metadata"] = _redact_provider_value(
            dict(raw_provider_metadata),
            config=None,
        )
    elif isinstance(response, Mapping):
        visual["raw_provider_metadata"] = {
            "response": _redact_provider_value(dict(response), config=None)
        }
    _copy_optional_visual_metadata(record, image, visual)
    visual["cache_key"] = image_cache_key(visual, source=source)
    return visual


def _missing_visual_claims(evidence: Mapping[str, Any], *, now: str) -> list[dict[str, Any]]:
    routes = evidence.get("routing", [])
    if not isinstance(routes, list):
        routes = []
    visual_required = [
        route
        for route in routes
        if isinstance(route, Mapping) and route.get("modality") == "visual_required"
    ]
    claims: list[dict[str, Any]] = []
    used_claim_ids = _existing_ids(evidence.get("claims", []))
    for index, route in enumerate(visual_required, start=1):
        angle_id = str(route.get("id") or f"angle_{index:03d}")
        claim_id = _unique_id(f"claim_needs_visual_{angle_id}", used_claim_ids)
        angle = str(route.get("angle") or angle_id)
        claims.append(
            {
                "id": claim_id,
                "text": f"Visual evidence is required for '{angle}' but no visual observation was provided.",
                "claim_type": "visual",
                "supporting_sources": [],
                "supporting_images": [],
                "quote_spans": [],
                "votes": [],
                "verification_status": "needs_visual_evidence",
                "review_status": "needs_more_evidence",
                "promotion_status": "not_eligible",
                "confidence": "low",
                "caveats": ["Generated by the vision adapter because a visual_required route had no visual result."],
                "angle_id": angle_id,
                "visual_tasks": list(route.get("visual_tasks", []))
                if isinstance(route.get("visual_tasks"), list)
                else [],
                "created_at": now,
                "extraction_stage": MISSING_VISUAL_STAGE,
            }
        )
    return claims


def _read_observation_records(
    run_dir: Path,
    observations: str | Path | None,
) -> tuple[list[Any], Path]:
    if observations is None:
        path = run_dir / "visual_observations.jsonl"
        if not path.exists():
            return [], path
    else:
        path = Path(observations)
    records: list[Any] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise VisionAdapterError(f"missing observations file: {path}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise VisionAdapterError(
                f"invalid JSONL record in {path} line {line_number}: {exc}"
            ) from exc
    return records, path


def _normalize_provider(provider: str) -> str:
    normalized = provider.strip().lower().replace("_", "-")
    if normalized not in VLM_PROVIDERS:
        raise VisionAdapterError("provider must be one of: " + ", ".join(VLM_PROVIDERS))
    return normalized


def _validated_optional_url(value: str | None, field_name: str, *, allow_file: bool) -> str | None:
    if value is None:
        return None
    if any(character.isspace() for character in value):
        raise VisionAdapterError(f"{field_name} contains whitespace")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise VisionAdapterError(f"{field_name} is malformed: {value}") from exc
    if parsed.scheme in {"http", "https"} and parsed.netloc and hostname:
        return value
    if allow_file and parsed.scheme == "file" and parsed.path:
        return value
    raise VisionAdapterError(f"{field_name} must be a valid http(s) URL")


def _file_uri_exists(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        return False
    return Path(unquote(parsed.path)).is_file()


def _origin(record: Mapping[str, Any], image: Mapping[str, Any], provider: str) -> str:
    origin = _first_optional_string(record, "origin") or _first_optional_string(image, "origin")
    if origin:
        return origin
    if provider == "manual-visual-review":
        return "manual"
    return "screenshot"


def _analysis_status(
    record: Mapping[str, Any],
    observations: Sequence[str],
    inferences: Sequence[str],
) -> str:
    raw = _first_optional_string(record, "analysis_status", "status")
    aliases = {
        "complete": "analyzed",
        "completed": "analyzed",
        "success": "analyzed",
        "error": "failed",
    }
    if raw:
        normalized = aliases.get(raw.strip().lower().replace("_", "-"), raw.strip().lower())
        if normalized in {"analyzed", "failed", "skipped", "needs_manual_review", "policy_blocked"}:
            return normalized
    if observations or inferences:
        return "analyzed"
    return "needs_manual_review"


def _mime_type(record: Mapping[str, Any], image: Mapping[str, Any], local_path: str) -> str:
    value = _first_optional_string(record, "mime_type") or _first_optional_string(image, "mime_type")
    if value:
        return value
    guessed, _ = mimetypes.guess_type(local_path)
    return guessed or "image/unknown"


def _hash(record: Mapping[str, Any], image: Mapping[str, Any], artifact_file: Path) -> str | None:
    value = _first_optional_string(record, "hash", "sha256") or _first_optional_string(
        image,
        "hash",
        "sha256",
    )
    if value:
        return value if value.startswith("sha256:") else "sha256:" + value
    if artifact_file.exists() and artifact_file.is_file():
        return "sha256:" + hashlib.sha256(artifact_file.read_bytes()).hexdigest()
    return None


def _artifact_size_bytes(
    record: Mapping[str, Any],
    image: Mapping[str, Any],
    artifact_file: Path,
) -> int | None:
    if artifact_file.exists() and artifact_file.is_file():
        return artifact_file.stat().st_size
    for container in (record, image):
        for key in ("artifact_size_bytes", "size_bytes", "content_length", "file_size", "byte_size"):
            value = container.get(key)
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, int):
                return value
            if isinstance(value, float) and value.is_integer():
                return int(value)
            if isinstance(value, str) and value.strip():
                try:
                    return int(value)
                except ValueError:
                    continue
    return None


def _ocr_outputs(
    record: Mapping[str, Any],
    image: Mapping[str, Any],
    *,
    ocr_text: str | None,
) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for container in (record, image):
        raw = container.get("ocr_outputs")
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
            for item in raw:
                if isinstance(item, Mapping):
                    text = _first_optional_string(item, "text", "ocr_text", "text_in_image")
                    if text:
                        entry = dict(item)
                        entry["text"] = text
                        outputs.append(entry)
                else:
                    text = str(item).strip()
                    if text:
                        outputs.append({"text": text})
    if ocr_text and not any(output.get("text") == ocr_text for output in outputs):
        outputs.append({"text": ocr_text})
    return outputs


def _copy_optional_visual_metadata(
    record: Mapping[str, Any],
    image: Mapping[str, Any],
    visual: dict[str, Any],
) -> None:
    scalar_keys = (
        "angle_id",
        "candidate_id",
        "candidate_class",
        "duplicate_of",
        "fetch_id",
        "near_duplicate_group_id",
        "near_duplicate_of",
        "observation_id",
        "pdf_local_path",
        "pdf_url",
        "plan_id",
        "provider",
        "provider_kind",
        "provider_mode",
        "provider_run_id",
        "policy_decision",
        "removal_reason",
        "route",
        "task_id",
        "visual_provider",
        "visual_acquisition_provider",
    )
    list_keys = ("removal_reasons",)
    number_keys = ("estimated_cost_usd", "actual_cost_usd", "page_number")
    mapping_keys = (
        "provider_provenance",
        "visual_validation",
        "validation_checks",
        "screenshot",
        "figure_hint",
        "rasterizer",
        "compute_counters",
        "cost_counters",
        "pdf_diagnostic",
    )
    for key in scalar_keys:
        value = _first_optional_string(record, key) or _first_optional_string(image, key)
        if value:
            visual[key] = _redact_provider_text(value, config=None)
    for key in list_keys:
        values = _dedupe(
            _redact_provider_text(value, config=None)
            for value in (_string_list(record, key) + _string_list(image, key))
        )
        if values:
            visual[key] = values
    for key in number_keys:
        for container in (record, image):
            value = container.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                visual[key] = value
                break
    for key in mapping_keys:
        value = record.get(key)
        if not isinstance(value, Mapping):
            value = image.get(key)
        if isinstance(value, Mapping):
            visual[key] = _redact_provider_value(dict(value), config=None)


def _phase3_observation_records(
    records: Sequence[Any],
    images: Sequence[Mapping[str, Any]],
    *,
    provider: str,
    now: str,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for index, image in enumerate(images):
        raw = records[index] if index < len(records) and isinstance(records[index], Mapping) else {}
        image_id = _first_optional_string(raw, "evidence_image_id") or str(image["id"])
        candidate_id = (
            _first_optional_string(raw, "candidate_id")
            or _first_optional_string(image, "candidate_id")
            or f"cand_{_sanitize_identifier(image_id)}"
        )
        angle_id = (
            _first_optional_string(raw, "angle_id")
            or _first_optional_string(image, "angle_id")
            or "angle_001"
        )
        task_id = (
            _first_optional_string(raw, "task_id")
            or _first_optional_string(image, "task_id")
            or f"task_visual_{_sanitize_identifier(angle_id.removeprefix('angle_'))}"
        )
        route = (
            _first_optional_string(raw, "route")
            or _first_optional_string(image, "route")
            or "visual_required"
        )
        plan_id = (
            _first_optional_string(raw, "plan_id")
            or _first_optional_string(image, "plan_id")
            or _plan_id_for_visual_task(task_id=task_id, angle_id=angle_id, route=route)
        )
        provider_name = (
            _first_optional_string(raw, "provider")
            or _first_optional_string(image, "provider", "visual_provider")
            or provider
        )
        provider_kind = (
            _first_optional_string(raw, "provider_kind")
            or _first_optional_string(image, "provider_kind")
            or "vlm"
        )
        provider_mode = (
            _first_optional_string(raw, "provider_mode")
            or _first_optional_string(image, "provider_mode")
            or "fixture"
        )
        provider_run_id = (
            _first_optional_string(raw, "provider_run_id")
            or _first_optional_string(image, "provider_run_id")
            or "vision-adapter-local"
        )
        phase3 = {
            "id": image["id"],
            "observation_id": _first_optional_string(raw, "observation_id")
            or _first_optional_string(image, "observation_id")
            or f"obs_{_sanitize_identifier(image_id)}",
            "evidence_image_id": image_id,
            "source_id": image["source_id"],
            "origin": image["origin"],
            "image_url": image.get("image_url"),
            "page_url": image.get("page_url"),
            "local_artifact_path": image["local_artifact_path"],
            "mime_type": image["mime_type"],
            "artifact_size_bytes": image.get("artifact_size_bytes"),
            "width": image["width"],
            "height": image["height"],
            "hash": image.get("hash"),
            "phash": image.get("phash"),
            "ocr_text": image.get("ocr_text"),
            "ocr_outputs": list(image.get("ocr_outputs", []))
            if isinstance(image.get("ocr_outputs"), list)
            else [],
            "observations": list(image.get("observations", []))
            if isinstance(image.get("observations"), list)
            else [],
            "inferences": list(image.get("inferences", []))
            if isinstance(image.get("inferences"), list)
            else [],
            "vlm_visual_summary": image.get("vlm_visual_summary"),
            "vlm_visual_description": image.get("vlm_visual_description"),
            "visual_tasks": list(image.get("visual_tasks", []))
            if isinstance(image.get("visual_tasks"), list)
            else [],
            "analysis_provider": image["analysis_provider"],
            "analysis_status": image["analysis_status"],
            "policy_flags": _dedupe(
                _string_list(raw, "policy_flags") + _string_list(image, "policy_flags")
            ),
            "caveats": list(image.get("caveats", []))
            if isinstance(image.get("caveats"), list)
            else [],
            "candidate_id": candidate_id,
            "fetch_id": _first_optional_string(raw, "fetch_id")
            or _first_optional_string(image, "fetch_id")
            or f"fetch_{_sanitize_identifier(candidate_id)}",
            "plan_id": plan_id,
            "task_id": task_id,
            "candidate_class": image.get("candidate_class"),
            "angle_id": angle_id,
            "route": route,
            "visual_provider": image.get("visual_provider") or provider_name,
            "provider": provider_name,
            "provider_kind": provider_kind,
            "provider_mode": provider_mode,
            "provider_run_id": provider_run_id,
            "provider_provenance": _provider_provenance(
                raw,
                image,
                provider=provider_name,
                provider_kind=provider_kind,
                provider_mode=provider_mode,
                provider_run_id=provider_run_id,
            ),
            "model_or_tool": _first_optional_string(raw, "model_or_tool") or provider,
            "observation_status": _first_optional_string(raw, "observation_status")
            or image.get("analysis_status", "analyzed"),
            "confidence": _first_number(raw, image, "confidence", default=1.0),
            "policy_decision": _first_optional_string(raw, "policy_decision")
            or _first_optional_string(image, "policy_decision")
            or "allowed",
            "verifier_links": _mapping_list(raw.get("verifier_links")),
            "report_links": _mapping_list(raw.get("report_links")),
            "estimated_cost_usd": _first_number(
                raw, image, "estimated_cost_usd", default=0.0
            ),
            "actual_cost_usd": _first_number(raw, image, "actual_cost_usd", default=0.0),
            "created_at": _first_optional_string(raw, "created_at") or now,
            "visual_validation": dict(image.get("visual_validation", {}))
            if isinstance(image.get("visual_validation"), Mapping)
            else {},
            "source_url": image.get("source_url"),
        }
        if isinstance(image.get("screenshot"), Mapping):
            phase3["screenshot"] = dict(image["screenshot"])
        _copy_optional_visual_metadata(raw, image, phase3)
        observations.append(phase3)
    return observations


def _provider_provenance(
    record: Mapping[str, Any],
    image: Mapping[str, Any],
    *,
    provider: str,
    provider_kind: str,
    provider_mode: str,
    provider_run_id: str,
) -> dict[str, Any]:
    for container in (record, image):
        value = container.get("provider_provenance")
        if isinstance(value, Mapping):
            return dict(value)
    return {
        "provider": provider,
        "provider_kind": provider_kind,
        "provider_mode": provider_mode,
        "provider_run_id": provider_run_id,
    }


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _first_number(
    record: Mapping[str, Any],
    image: Mapping[str, Any],
    key: str,
    *,
    default: float,
) -> float | int:
    for container in (record, image):
        value = container.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return default


def _sanitize_identifier(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower()
    return normalized or "visual"


def _number(record: Mapping[str, Any], image: Mapping[str, Any], key: str) -> int | float:
    for container in (record, image):
        value = container.get(key)
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str) and value.strip():
            try:
                numeric = float(value)
            except ValueError:
                continue
            return int(numeric) if numeric.is_integer() else numeric
    return 0


def _automated_analysis_provider_status(analysis: _AutomatedVisionAnalysis) -> str:
    if analysis.provider == OPENAI_RESPONSES_VISION_PROVIDER:
        return "openai_responses_vision_analyzed"
    if analysis.provider == CODEX_INTERACTIVE_PROVIDER:
        return "codex_interactive_visual_worker_analyzed"
    return "visual_worker_analyzed"


def _automated_analysis_actionable_cause(analysis: _AutomatedVisionAnalysis) -> str:
    if analysis.provider == OPENAI_RESPONSES_VISION_PROVIDER:
        return "openai-responses-vision analyzed fetched visual artifacts"
    if analysis.provider == CODEX_INTERACTIVE_PROVIDER:
        return (
            "codex-interactive visual worker analyzed fetched visual artifacts "
            "via explicit --image handoff"
        )
    return "visual worker analyzed fetched visual artifacts"


def _vision_trace_tool_call_summary(
    automated_analysis: _AutomatedVisionAnalysis | None,
) -> str:
    if automated_analysis is None:
        return (
            "Read visual observations, reused unchanged image cache hits, normalized "
            "changed image evidence, and updated evidence.json without VLM calls."
        )
    if automated_analysis.provider == CODEX_INTERACTIVE_PROVIDER:
        return (
            "Invoked codex exec --json --image for fetched local image artifacts, "
            "wrote visual observations, and normalized image evidence."
        )
    if automated_analysis.provider == OPENAI_RESPONSES_VISION_PROVIDER:
        return (
            "Invoked openai-responses-vision for fetched visual artifacts, wrote "
            "visual observations, and normalized image evidence."
        )
    return (
        "Invoked automated visual worker, wrote visual observations, and normalized "
        "image evidence."
    )


def _first_string(container: Mapping[str, Any], *keys: str) -> str | None:
    value = _first_optional_string(container, *keys)
    if value:
        return value
    return None


def _first_optional_string(container: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = container.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            return value.strip() or None
        return str(value)
    return None


def _string_list(container: Mapping[str, Any], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        raw = container.get(key)
        if raw is None:
            continue
        if isinstance(raw, str):
            if raw.strip():
                values.append(raw.strip())
            continue
        if isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
            for value in raw:
                if value is None:
                    continue
                text = str(value).strip()
                if text:
                    values.append(text)
            continue
        text = str(raw).strip()
        if text:
            values.append(text)
    return values


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _existing_ids(records: Any) -> set[str]:
    if not isinstance(records, list):
        return set()
    return {
        record["id"]
        for record in records
        if isinstance(record, Mapping) and isinstance(record.get("id"), str)
    }


def _sources_by_id(records: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(records, list):
        return {}
    return {
        record["id"]: record
        for record in records
        if isinstance(record, Mapping) and isinstance(record.get("id"), str)
    }


def _reuse_completed_images(
    existing_images: list[Any],
    normalized_images: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    by_id: dict[str, Mapping[str, Any]] = {}
    by_cache_key: dict[str, Mapping[str, Any]] = {}
    for image in existing_images:
        if not isinstance(image, Mapping):
            continue
        image_id = image.get("id")
        cache_key = image.get("cache_key")
        if isinstance(image_id, str):
            by_id[image_id] = image
        if isinstance(cache_key, str):
            by_cache_key[cache_key] = image

    reused_count = 0
    result: list[dict[str, Any]] = []
    for image in normalized_images:
        current_key = image.get("cache_key")
        existing = None
        image_id = image.get("id")
        if isinstance(image_id, str):
            existing = by_id.get(image_id)
        if existing is None and isinstance(current_key, str):
            existing = by_cache_key.get(current_key)
        if _can_reuse_image(existing, image):
            reused = dict(existing)
            reused["cache_key"] = current_key
            if image.get("artifact_size_bytes") is not None:
                reused.setdefault("artifact_size_bytes", image["artifact_size_bytes"])
            if image.get("source_url") is not None:
                reused.setdefault("source_url", image["source_url"])
            result.append(reused)
            reused_count += 1
            continue
        result.append(image)
    return result, reused_count


def _can_reuse_image(existing: Mapping[str, Any] | None, image: Mapping[str, Any]) -> bool:
    current_key = image.get("cache_key")
    if not isinstance(existing, Mapping) or not isinstance(current_key, str):
        return False
    previous_key = existing.get("cache_key")
    if isinstance(previous_key, str):
        if previous_key != current_key:
            return False
    elif image_cache_key(existing) != current_key:
        return False
    return existing.get("analysis_status") in {
        "analyzed",
        "needs_manual_review",
        "policy_blocked",
        "skipped",
    }


def _image_id(
    value: str | None,
    *,
    index: int,
    existing_image_ids: set[str],
    used_image_ids: set[str],
) -> str:
    if value is not None:
        return _unique_id("img_" + _safe_id(value.removeprefix("img_")), used_image_ids)
    base = f"img_visual_{index + 1:03d}"
    return _unique_id(base, set(existing_image_ids) | used_image_ids)


def _unique_id(value: str, used_ids: set[str]) -> str:
    base = _safe_id(value)
    candidate = base
    suffix = 1
    while candidate in used_ids:
        suffix += 1
        candidate = f"{base}_{suffix}"
    used_ids.add(candidate)
    return candidate


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return safe or "visual"


def _resolve_run_relative_path(run_dir: Path, relative_path: str | Path) -> Path:
    path = Path(relative_path)
    if path.is_absolute():
        raise VisionAdapterError(f"artifact path must be run-relative: {relative_path}")
    if any(part == ".." for part in path.parts):
        raise VisionAdapterError(f"artifact path cannot traverse outside run directory: {relative_path}")
    root = run_dir.resolve()
    target = (root / path).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise VisionAdapterError(
            f"artifact path resolves outside run directory: {relative_path}"
        ) from exc
    return target


def _relative_or_string(run_dir: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(run_dir.resolve()).as_posix()
    except ValueError:
        return str(path)


def _base_status(run_dir: Path, evidence: Mapping[str, Any], status: str) -> dict[str, Any]:
    return {
        "schema_version": VISION_ADAPTER_SCHEMA_VERSION,
        "run_id": evidence.get("run_id", run_dir.name),
        "run_dir": str(run_dir),
        "status": status,
        "created_at": _utc_now(),
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VisionAdapterError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise VisionAdapterError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise VisionAdapterError(f"expected JSON object in {path}")
    return payload


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
