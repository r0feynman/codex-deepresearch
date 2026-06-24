"""Manual source ingestion for local-only DeepResearch fallback runs."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .evidence_schema import EVIDENCE_SCHEMA_VERSION, validate_artifacts
from .execution_mode import BudgetPreset, resolve_config
from .run_state import begin_stage, skip_stage, skipped_stage_status
from .search_handoff import resolve_run_dir
from .trace import record_stage_trace


MANUAL_INGEST_SCHEMA_VERSION = "codex-deepresearch.manual-ingest.v0"


class ManualSourcesError(ValueError):
    """Raised when manual source ingestion cannot continue."""


@dataclass(frozen=True)
class _ManualInput:
    kind: str
    value: str
    label: str | None


def ingest_manual_sources(
    *,
    run: str | Path | None = None,
    runs_dir: str | Path,
    question: str | None = None,
    urls: Sequence[str] = (),
    pdfs: Sequence[str | Path] = (),
    image_urls: Sequence[str] = (),
    local_images: Sequence[str | Path] = (),
    labels: Sequence[str] = (),
    budget_preset: str = "standard",
    vlm_provider: str | None = None,
) -> dict[str, Any]:
    """Create or update evidence with user-provided sources and images.

    This path is intentionally local-only. It records caller-provided metadata and
    local image bytes, but it does not call search, fetch remote bodies, or run VLM
    analysis.
    """

    manual_inputs = _build_manual_inputs(
        urls=urls,
        pdfs=pdfs,
        image_urls=image_urls,
        local_images=local_images,
        labels=labels,
    )
    if not manual_inputs:
        raise ManualSourcesError("at least one manual source input is required")

    now = _utc_now()
    if run is None:
        run_dir, evidence = _create_manual_run(
            runs_dir=Path(runs_dir),
            question=question,
            budget_preset=budget_preset,
            vlm_provider=vlm_provider,
            created_at=now,
        )
        for skipped_stage in ("planning", "ingest", "fetch_claims", "ingest_vision"):
            skip_stage(
                run_dir,
                skipped_stage,
                reason="manual_sources_run",
                timestamp=now,
                run_id=evidence.get("run_id", run_dir.name),
            )
    else:
        run_dir = resolve_run_dir(run, runs_dir=runs_dir)
        evidence_path = run_dir / "evidence.json"
        if not evidence_path.exists():
            raise ManualSourcesError(f"missing evidence.json in run directory: {run_dir}")
        evidence = _read_json(evidence_path)
        if question is not None and not evidence.get("question"):
            normalized_question = question.strip()
            if normalized_question:
                evidence["question"] = normalized_question

    start = begin_stage(
        run_dir,
        "ingest_manual",
        run_id=str(evidence.get("run_id", run_dir.name)),
        started_at=now,
        completed_behavior="skip",
    )
    if start.skipped:
        status = skipped_stage_status(
            run_dir,
            stage="ingest_manual",
            schema_version=MANUAL_INGEST_SCHEMA_VERSION,
            status_artifact_key="manual_ingest_status",
            status_filename="manual_ingest_status.json",
            reason=start.skip_reason or "stage_already_completed",
            run_id=str(evidence.get("run_id", run_dir.name)),
        )
        record_stage_trace(
            run_dir,
            stage="ingest_manual",
            agent_role="manual_source_ingest_agent",
            status_payload=status,
            prompt_summary="Record user-provided source and image metadata without external fetches.",
            tool_call_summary="Skipped manual ingestion because run_steps.json marks the stage terminal.",
        )
        _write_json(run_dir / "manual_ingest_status.json", status)
        return status

    sources = evidence.setdefault("sources", [])
    images = evidence.setdefault("images", [])
    if not isinstance(sources, list):
        raise ManualSourcesError("evidence.sources must be a list")
    if not isinstance(images, list):
        raise ManualSourcesError("evidence.images must be a list")

    used_source_ids = {
        source["id"]
        for source in sources
        if isinstance(source, Mapping) and isinstance(source.get("id"), str)
    }
    used_image_ids = {
        image["id"]
        for image in images
        if isinstance(image, Mapping) and isinstance(image.get("id"), str)
    }

    sources_dir = run_dir / "sources"
    images_dir = run_dir / "images"
    sources_dir.mkdir(exist_ok=True)
    images_dir.mkdir(exist_ok=True)

    added_sources: list[dict[str, Any]] = []
    added_images: list[dict[str, Any]] = []
    for index, manual_input in enumerate(manual_inputs, start=1):
        source = _source_from_manual_input(
            manual_input,
            index=index,
            created_at=now,
            used_source_ids=used_source_ids,
        )
        _write_json(run_dir / source["local_artifact_path"], source)
        sources.append(source)
        added_sources.append(source)

        if manual_input.kind in {"image_url", "local_image"}:
            image = _visual_evidence_from_manual_input(
                manual_input,
                source=source,
                index=index,
                used_image_ids=used_image_ids,
                images_dir=images_dir,
                run_dir=run_dir,
                analysis_provider=str(evidence.get("vlm_provider", "manual-visual-review")),
            )
            if image["local_artifact_path"].endswith(".json"):
                _write_json(run_dir / image["local_artifact_path"], image)
            images.append(image)
            added_images.append(image)

    evidence["manual_ingest"] = {
        "schema_version": MANUAL_INGEST_SCHEMA_VERSION,
        "status": "manual_sources_ingested",
        "ingested_at": now,
        "sources_ingested": len(added_sources),
        "images_ingested": len(added_images),
        "external_search": False,
        "body_fetch": False,
        "evidence_source": _manual_evidence_source(len(added_sources), len(added_images)),
    }
    _write_json(run_dir / "evidence.json", evidence)

    validation = validate_artifacts(evidence_path=run_dir / "evidence.json")
    status = {
        "schema_version": MANUAL_INGEST_SCHEMA_VERSION,
        "run_id": evidence.get("run_id", run_dir.name),
        "run_dir": str(run_dir),
        "status": "manual_sources_ingested" if validation.valid else "failed_validation",
        "created_at": now,
        "sources_ingested": len(added_sources),
        "images_ingested": len(added_images),
        "external_search": False,
        "body_fetch": False,
        "evidence_source": _manual_evidence_source(len(added_sources), len(added_images)),
        "validation": validation.to_dict(),
        "artifacts": {
            "evidence": str(run_dir / "evidence.json"),
            "manual_ingest_status": str(run_dir / "manual_ingest_status.json"),
        },
    }
    record_stage_trace(
        run_dir,
        stage="ingest_manual",
        agent_role="manual_source_ingest_agent",
        status_payload=status,
        prompt_summary="Record user-provided source and image metadata without external fetches.",
        tool_call_summary="Copied or referenced manual inputs, updated evidence.json, and wrote manual_ingest_status.json.",
    )
    _write_json(run_dir / "manual_ingest_status.json", status)
    return status


def _manual_evidence_source(sources_ingested: int, images_ingested: int) -> dict[str, Any]:
    return {
        "type": "manual_handoff",
        "adapter": "manual-sources",
        "sources_ingested": sources_ingested,
        "images_ingested": images_ingested,
        "fixture_only": False,
        "manual_handoff": True,
        "real_child_execution": False,
        "real_use_e2e_eligible": False,
        "description": "user-provided manual handoff sources; no fixture or Codex child execution evidence",
    }


def _build_manual_inputs(
    *,
    urls: Sequence[str],
    pdfs: Sequence[str | Path],
    image_urls: Sequence[str],
    local_images: Sequence[str | Path],
    labels: Sequence[str],
) -> list[_ManualInput]:
    values: list[tuple[str, str]] = []
    values.extend(("url", str(value)) for value in urls)
    values.extend(("pdf", str(value)) for value in pdfs)
    values.extend(("image_url", str(value)) for value in image_urls)
    values.extend(("local_image", str(value)) for value in local_images)
    if len(labels) > len(values):
        raise ManualSourcesError("--label was provided more times than manual inputs")

    manual_inputs: list[_ManualInput] = []
    for index, (kind, value) in enumerate(values):
        normalized_value = value.strip()
        if not normalized_value:
            raise ManualSourcesError(f"{kind} input cannot be empty")
        label = labels[index].strip() if index < len(labels) else None
        if label == "":
            raise ManualSourcesError("--label values cannot be empty")
        manual_inputs.append(_ManualInput(kind=kind, value=normalized_value, label=label))
    return manual_inputs


def _create_manual_run(
    *,
    runs_dir: Path,
    question: str | None,
    budget_preset: str,
    vlm_provider: str | None,
    created_at: str,
) -> tuple[Path, dict[str, Any]]:
    normalized_question = (question or "").strip()
    if not normalized_question:
        raise ManualSourcesError("question is required when creating a new manual run")

    config = resolve_config(
        mode="manual-sources",
        search_provider="manual",
        vlm_provider=vlm_provider,
        budget_preset=budget_preset,
    )
    run_dir = _create_unique_run_dir(runs_dir, created_at)
    evidence = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "created_at": created_at,
        "question": normalized_question,
        "mode": config.mode,
        "search_provider": config.search_provider,
        "vlm_provider": config.vlm_provider,
        "routing": [],
        "search_tasks": [],
        "budget": _budget_to_evidence(config.budget_preset, config.budget),
        "sources": [],
        "images": [],
        "claims": [],
        "manual_sources": {
            "schema_version": MANUAL_INGEST_SCHEMA_VERSION,
            "status": "created",
            "created_at": created_at,
            "external_search": False,
            "body_fetch": False,
        },
    }
    return run_dir, evidence


def _source_from_manual_input(
    manual_input: _ManualInput,
    *,
    index: int,
    created_at: str,
    used_source_ids: set[str],
) -> dict[str, Any]:
    source_id = _unique_id("src_manual", used_source_ids)
    source_url = _source_url(manual_input)
    source = {
        "id": source_id,
        "type": _source_type(manual_input),
        "url": source_url,
        "title": _title(manual_input, index),
        "published_at": None,
        "accessed_at": created_at,
        "quality": "unknown",
        "retrieval_status": "manual",
        "local_artifact_path": f"sources/{source_id}.json",
        "license_policy": "manual_review",
        "robots_policy": "manual_review" if _is_http_url(source_url) else "unknown",
        "origin": "manual",
        "provider": "manual",
        "manual_input_kind": manual_input.kind,
        "policy_decision": "manual_review",
        "policy_flags": ["manual_source"],
        "retrieval_metadata": {
            "method": "manual-input",
            "recorded_at": created_at,
            "external_search": False,
            "body_fetch": False,
        },
        "raw_provider_metadata": {},
    }
    return source


def _visual_evidence_from_manual_input(
    manual_input: _ManualInput,
    *,
    source: Mapping[str, Any],
    index: int,
    used_image_ids: set[str],
    images_dir: Path,
    run_dir: Path,
    analysis_provider: str,
) -> dict[str, Any]:
    image_id = _unique_id("img_manual", used_image_ids)
    source_url = str(source["url"])

    if manual_input.kind == "local_image":
        local_path = Path(manual_input.value).expanduser().resolve(strict=True)
        metadata = _local_image_metadata(local_path)
        copied_path = images_dir / f"{image_id}{_image_suffix(local_path, metadata['mime_type'])}"
        shutil.copyfile(local_path, copied_path)
        artifact_path = copied_path.relative_to(run_dir).as_posix()
        mime_type = metadata["mime_type"]
        width = metadata["width"]
        height = metadata["height"]
        caveats = list(metadata["caveats"])
        image_url = local_path.as_uri()
        origin = "user_upload"
        image_hash = metadata["hash"]
    else:
        _require_http_url(manual_input.value, "image URL")
        artifact_path = f"images/{image_id}.json"
        mime_type = _guess_mime_type(manual_input.value, default="image/unknown")
        width = 0
        height = 0
        caveats = ["image URL recorded without fetching bytes; dimensions unavailable"]
        image_url = manual_input.value
        origin = "manual"
        image_hash = None

    image = {
        "id": image_id,
        "source_id": source["id"],
        "origin": origin,
        "image_url": image_url,
        "page_url": None,
        "local_artifact_path": artifact_path,
        "mime_type": mime_type,
        "width": width,
        "height": height,
        "hash": image_hash,
        "phash": None,
        "observations": [],
        "inferences": [],
        "visual_tasks": [],
        "analysis_provider": analysis_provider,
        "analysis_status": "skipped",
        "policy_flags": ["manual_source"],
        "caveats": caveats,
        "manual_input_kind": manual_input.kind,
        "source_title": source["title"],
        "rank": index,
    }
    return image


def _source_url(manual_input: _ManualInput) -> str:
    if manual_input.kind in {"url", "image_url"}:
        _require_http_url(manual_input.value, manual_input.kind.replace("_", " "))
        return manual_input.value
    if manual_input.kind == "pdf":
        if _is_http_url(manual_input.value):
            return manual_input.value
        path = Path(manual_input.value).expanduser()
        if path.exists():
            return path.resolve().as_uri()
        raise ManualSourcesError("pdf input must be an http(s) URL or an existing local file")
    if manual_input.kind == "local_image":
        path = Path(manual_input.value).expanduser().resolve(strict=True)
        return path.as_uri()
    raise ManualSourcesError(f"unsupported manual input kind: {manual_input.kind}")


def _source_type(manual_input: _ManualInput) -> str:
    if manual_input.kind == "pdf":
        return "pdf"
    if manual_input.kind in {"image_url", "local_image"}:
        return "image"
    return "web"


def _title(manual_input: _ManualInput, index: int) -> str:
    if manual_input.label:
        return manual_input.label
    if manual_input.kind == "local_image":
        path = Path(manual_input.value)
        return path.stem or path.name or f"Manual image {index}"
    parsed = urlparse(manual_input.value)
    name = Path(parsed.path).name
    if name:
        return name
    return f"Manual {manual_input.kind.replace('_', ' ')} {index}"


def _local_image_metadata(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    mime_type = _detect_image_mime(data) or _guess_mime_type(path.name, default="image/unknown")
    width, height = _detect_image_dimensions(data)
    caveats: list[str] = []
    if width == 0 or height == 0:
        caveats.append("image dimensions unavailable from local headers")
    return {
        "mime_type": mime_type,
        "width": width,
        "height": height,
        "hash": "sha256:" + hashlib.sha256(data).hexdigest(),
        "caveats": caveats,
    }


def _detect_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _detect_image_dimensions(data: bytes) -> tuple[int, int]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data.startswith(b"\xff\xd8"):
        dimensions = _jpeg_dimensions(data)
        if dimensions is not None:
            return dimensions
    return 0, 0


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    index = 2
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while index < len(data):
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            return None
        marker = data[index]
        index += 1
        if marker in {0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9}:
            continue
        if index + 2 > len(data):
            return None
        segment_length = int.from_bytes(data[index : index + 2], "big")
        if segment_length < 2 or index + segment_length > len(data):
            return None
        if marker in sof_markers and segment_length >= 7:
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            return width, height
        index += segment_length
    return None


def _image_suffix(path: Path, mime_type: str) -> str:
    if path.suffix:
        return path.suffix
    if mime_type == "image/png":
        return ".png"
    if mime_type == "image/jpeg":
        return ".jpg"
    if mime_type == "image/gif":
        return ".gif"
    if mime_type == "image/webp":
        return ".webp"
    return ".bin"


def _guess_mime_type(value: str, *, default: str) -> str:
    guessed, _ = mimetypes.guess_type(value)
    if guessed:
        return guessed
    return default


def _require_http_url(url: str, label: str) -> None:
    if not _is_http_url(url):
        raise ManualSourcesError(f"{label} must be an http(s) URL")


def _is_http_url(url: str) -> bool:
    if not url or any(character.isspace() for character in url):
        return False
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and bool(hostname)


def _budget_to_evidence(preset: str, budget: BudgetPreset) -> dict[str, Any]:
    data = asdict(budget)
    data["preset"] = preset
    data["verifier_invocations_used"] = 0
    data["model_api_calls_used"] = 0
    data["sources_selected"] = 0
    data["images_selected"] = 0
    return data


def _create_unique_run_dir(runs_dir: Path, created_at: str) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = created_at.replace("-", "").replace(":", "").rstrip("Z")
    run_dir = runs_dir / f"dr_manual_{timestamp}"
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = runs_dir / f"dr_manual_{timestamp}_{suffix}"
    run_dir.mkdir()
    return run_dir.resolve()


def _unique_id(prefix: str, used_ids: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_]+", "_", prefix).strip("_") or "manual"
    suffix = 1
    candidate = f"{base}_{suffix:03d}"
    while candidate in used_ids:
        suffix += 1
        candidate = f"{base}_{suffix:03d}"
    used_ids.add(candidate)
    return candidate


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManualSourcesError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManualSourcesError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManualSourcesError(f"expected JSON object in {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
