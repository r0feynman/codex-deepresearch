"""Normalize visual analysis handoff records into VisualEvidence."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlparse

from .cache_keys import image_cache_key
from .evidence_schema import VLM_PROVIDERS, validate_artifacts
from .run_state import begin_stage, skipped_stage_status
from .search_handoff import resolve_run_dir
from .trace import record_stage_trace


VISION_ADAPTER_SCHEMA_VERSION = "codex-deepresearch.vision-adapter.v0"
MISSING_VISUAL_STAGE = "vision_adapter_missing_visual"
VISION_ADAPTER_STAGE = "vision_adapter"


class VisionAdapterError(ValueError):
    """Raised when visual observations cannot be normalized."""


def ingest_vision_observations(
    *,
    run: str | Path,
    provider: str,
    observations: str | Path | None = None,
    runs_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Normalize provider-specific visual observations into run evidence.

    The adapter is intentionally dry: it reads already-produced artifacts and
    never calls a VLM, Codex hidden API, or external network service.
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

    evidence["vlm_provider"] = normalized_provider
    evidence.setdefault("images", [])
    evidence.setdefault("claims", [])
    if not isinstance(evidence["images"], list):
        raise VisionAdapterError("evidence.images must be a list")
    if not isinstance(evidence["claims"], list):
        raise VisionAdapterError("evidence.claims must be a list")

    visual_observations_path = run_dir / "visual_observations.jsonl"
    errors: list[dict[str, Any]] = []
    normalized_images: list[dict[str, Any]] = []
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
        _write_jsonl(visual_observations_path, normalized_images)
        adapter_status = "visual_evidence_ingested"
    else:
        missing_claims = _missing_visual_claims(evidence, now=now)
        evidence["claims"].extend(missing_claims)
        _write_jsonl(visual_observations_path, [])
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
        "external_vlm_call": False,
    }
    handoff = evidence.get("handoff")
    if isinstance(handoff, dict):
        handoff["visual_observations_path"] = "visual_observations.jsonl"
        handoff["visual_status"] = adapter_status

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
    record_stage_trace(
        run_dir,
        stage="ingest_vision",
        agent_role="vision_adapter",
        status_payload=status,
        prompt_summary="Normalize visual handoff records into VisualEvidence.",
        tool_call_summary="Read visual observations, reused unchanged image cache hits, normalized changed image evidence, and updated evidence.json without VLM calls.",
    )
    _write_json(run_dir / "vision_ingest_status.json", status)
    return status


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

    ocr_text = _first_optional_string(record, "ocr_text") or _first_optional_string(image, "ocr_text")
    if ocr_text:
        observations.append(ocr_text)

    observations = _dedupe(observations)
    inferences = _dedupe(inferences)
    caveats = _dedupe(_string_list(record, "caveats") + _string_list(image, "caveats"))
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
        "observations": observations,
        "inferences": inferences,
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
        visual["raw_provider_metadata"] = dict(raw_provider_metadata)
    elif isinstance(response, Mapping):
        visual["raw_provider_metadata"] = {"response": dict(response)}
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
        if _can_reuse_image(existing, current_key):
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


def _can_reuse_image(existing: Mapping[str, Any] | None, current_key: Any) -> bool:
    if not isinstance(existing, Mapping) or not isinstance(current_key, str):
        return False
    previous_key = existing.get("cache_key")
    if isinstance(previous_key, str) and previous_key != current_key:
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
