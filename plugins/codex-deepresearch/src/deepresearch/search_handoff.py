"""Codex-native search handoff preparation and ingestion."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .evidence_schema import (
    EVIDENCE_SCHEMA_VERSION,
    FRESHNESS_REQUIREMENTS,
    POLICY_DECISIONS,
    SEARCH_ROUTES,
    validate_artifacts,
)
from .execution_mode import BudgetPreset, resolve_config


HANDOFF_SCHEMA_VERSION = "codex-deepresearch.search-handoff.v0"
INGEST_STATUS_SCHEMA_VERSION = "codex-deepresearch.search-ingest.v0"
FETCH_QUEUE_SCHEMA_VERSION = "codex-deepresearch.fetch-queue.v0"


class SearchHandoffError(ValueError):
    """Raised when search handoff preparation or ingestion cannot continue."""


def prepare_run(
    *,
    question: str,
    runs_dir: str | Path,
    mode: str = "codex-plugin",
    search_provider: str | None = "codex-native",
    vlm_provider: str | None = None,
    budget_preset: str = "standard",
    freshness_requirement: str = "any",
    route: str = "text_only",
    max_results: int = 8,
) -> dict[str, Any]:
    """Create a run directory and the Codex search handoff artifacts."""

    normalized_question = question.strip()
    if not normalized_question:
        raise SearchHandoffError("question cannot be empty")
    if freshness_requirement not in FRESHNESS_REQUIREMENTS:
        raise SearchHandoffError(
            "freshness_requirement must be one of: "
            + ", ".join(FRESHNESS_REQUIREMENTS)
        )
    if route not in SEARCH_ROUTES:
        raise SearchHandoffError("route must be one of: " + ", ".join(SEARCH_ROUTES))
    if max_results < 1:
        raise SearchHandoffError("max_results must be at least 1")

    config = resolve_config(
        mode=mode,
        search_provider=search_provider,
        vlm_provider=vlm_provider,
        budget_preset=budget_preset,
    )
    if config.mode != "codex-plugin" or config.search_provider != "codex-native":
        raise SearchHandoffError(
            "search handoff prepare is only implemented for codex-plugin mode "
            "with codex-native search"
        )

    now = _utc_now()
    run_dir = _create_unique_run_dir(Path(runs_dir), now)
    run_id = run_dir.name
    routing = [
        {
            "id": "angle_001",
            "angle": "primary source discovery",
            "modality": route,
            "reason": "Initial Codex-native search handoff for the research question.",
            "visual_tasks": [] if route == "text_only" else ["image_claim_alignment"],
            "max_images": 0 if route == "text_only" else config.budget.max_images,
        }
    ]
    search_tasks = [
        {
            "id": "task_search_001",
            "angle_id": "angle_001",
            "angle": routing[0]["angle"],
            "query": normalized_question,
            "freshness_requirement": freshness_requirement,
            "modality": route,
            "route": route,
            "max_results": max_results,
            "source_policy": {
                "decision": "allowed",
                "flags": [],
            },
        }
    ]
    budget = _budget_to_evidence(config.budget_preset, config.budget)
    evidence = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": now,
        "question": normalized_question,
        "mode": config.mode,
        "search_provider": config.search_provider,
        "vlm_provider": config.vlm_provider,
        "routing": routing,
        "search_tasks": search_tasks,
        "budget": budget,
        "sources": [],
        "images": [],
        "claims": [],
        "handoff": {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "status": "awaiting_search_results",
            "search_results_path": "search_results.jsonl",
            "visual_observations_path": "visual_observations.jsonl",
        },
    }

    search_tasks_artifact = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": now,
        "question": normalized_question,
        "mode": config.mode,
        "search_provider": config.search_provider,
        "tasks": search_tasks,
    }
    visual_tasks_artifact = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": now,
        "tasks": [],
    }
    status = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "run_id": run_id,
        "status": "awaiting_search_results",
        "created_at": now,
        "question": normalized_question,
        "mode": config.mode,
        "search_provider": config.search_provider,
        "vlm_provider": config.vlm_provider,
        "next_step": (
            "Fill search_results.jsonl with SearchResult records, then run "
            "codex-deepresearch ingest --run <run_id_or_path>."
        ),
    }

    _write_json(run_dir / "evidence.json", evidence)
    _write_json(run_dir / "search_tasks.json", search_tasks_artifact)
    (run_dir / "search_results.jsonl").write_text("", encoding="utf-8")
    _write_json(run_dir / "visual_tasks.json", visual_tasks_artifact)
    (run_dir / "visual_observations.jsonl").write_text("", encoding="utf-8")
    _write_json(run_dir / "status.json", status)

    validation = validate_artifacts(evidence_path=run_dir / "evidence.json")
    if not validation.valid:
        raise SearchHandoffError(
            "prepared evidence.json failed validation: "
            + json.dumps(validation.to_dict(), sort_keys=True)
        )

    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "status": "awaiting_search_results",
        "artifacts": {
            "evidence": str(run_dir / "evidence.json"),
            "search_tasks": str(run_dir / "search_tasks.json"),
            "search_results": str(run_dir / "search_results.jsonl"),
            "visual_tasks": str(run_dir / "visual_tasks.json"),
            "visual_observations": str(run_dir / "visual_observations.jsonl"),
            "status": str(run_dir / "status.json"),
        },
    }


def ingest_run(
    *,
    run: str | Path,
    runs_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Validate and ingest Codex-native SearchResult records for one run."""

    run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    evidence_path = run_dir / "evidence.json"
    search_results_path = run_dir / "search_results.jsonl"
    if not evidence_path.exists():
        raise SearchHandoffError(f"missing evidence.json in run directory: {run_dir}")
    if not search_results_path.exists():
        return _write_blocked_status(run_dir, "missing_search_results_file")
    if not search_results_path.read_text(encoding="utf-8").strip():
        return _write_blocked_status(run_dir, "empty_search_results_file")

    validation = validate_artifacts(
        evidence_path=evidence_path,
        search_results_path=search_results_path,
    )
    if not validation.valid:
        status = _base_ingest_status(run_dir, "failed_validation")
        status["validation"] = validation.to_dict()
        _write_json(run_dir / "ingest_status.json", status)
        return status

    evidence = _read_json(evidence_path)
    records = _read_jsonl(search_results_path)
    now = _utc_now()
    sources_dir = run_dir / "sources"
    sources_dir.mkdir(exist_ok=True)

    normalized_sources: list[dict[str, Any]] = []
    fetch_entries: list[dict[str, Any]] = []
    ingest_errors: list[dict[str, Any]] = []
    used_source_ids: set[str] = set()
    for index, record in enumerate(records):
        source, fetchable, error = _source_from_search_result(
            record,
            index=index,
            used_source_ids=used_source_ids,
        )
        normalized_sources.append(source)
        if error is not None:
            ingest_errors.append(error)
        if fetchable:
            fetch_entries.append(_fetch_queue_entry(source))
        _write_json(run_dir / source["local_artifact_path"], source)

    current_sources = evidence.get("sources", [])
    if not isinstance(current_sources, list):
        current_sources = []
    result_ids = {record["id"] for record in records}
    generated_ids = {source["id"] for source in normalized_sources}
    kept_sources = [
        source
        for source in current_sources
        if not (
            isinstance(source, Mapping)
            and (
                source.get("search_result_id") in result_ids
                or source.get("id") in generated_ids
            )
        )
    ]
    evidence["sources"] = kept_sources + normalized_sources
    evidence["handoff"] = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "status": "search_results_ingested",
        "search_results_path": "search_results.jsonl",
        "visual_observations_path": "visual_observations.jsonl",
    }
    evidence["ingest"] = {
        "schema_version": INGEST_STATUS_SCHEMA_VERSION,
        "status": "ingested_with_rejections" if ingest_errors else "ingested",
        "ingested_at": now,
        "search_results_path": "search_results.jsonl",
        "fetch_queue_path": "fetch_queue.json",
        "sources_ingested": len(normalized_sources),
        "fetch_queue_count": len(fetch_entries),
        "errors": ingest_errors,
    }
    _write_json(evidence_path, evidence)

    fetch_queue = {
        "schema_version": FETCH_QUEUE_SCHEMA_VERSION,
        "run_id": evidence.get("run_id"),
        "created_at": now,
        "entries": fetch_entries,
    }
    _write_json(run_dir / "fetch_queue.json", fetch_queue)

    evidence_validation = validate_artifacts(evidence_path=evidence_path)
    status = _base_ingest_status(
        run_dir,
        "ingested_with_rejections" if ingest_errors else "ingested",
    )
    status.update(
        {
            "validation": evidence_validation.to_dict(),
            "sources_ingested": len(normalized_sources),
            "fetch_queue_count": len(fetch_entries),
            "errors": ingest_errors,
            "artifacts": {
                "evidence": str(evidence_path),
                "fetch_queue": str(run_dir / "fetch_queue.json"),
                "ingest_status": str(run_dir / "ingest_status.json"),
            },
        }
    )
    _write_json(run_dir / "ingest_status.json", status)
    return status


def resolve_run_dir(run: str | Path, *, runs_dir: str | Path | None = None) -> Path:
    """Resolve a run id or direct run path to an existing run directory."""

    run_path = Path(run)
    if run_path.exists():
        if not run_path.is_dir():
            raise SearchHandoffError(f"run path is not a directory: {run_path}")
        return run_path.resolve()
    if run_path.is_absolute():
        raise SearchHandoffError(f"run directory does not exist: {run_path}")
    if runs_dir is not None:
        candidate = Path(runs_dir) / run_path
        if candidate.exists() and candidate.is_dir():
            return candidate.resolve()
    raise SearchHandoffError(
        f"run '{run}' was not found as a path"
        + (" or under the configured runs directory" if runs_dir is not None else "")
    )


def _write_blocked_status(run_dir: Path, reason: str) -> dict[str, Any]:
    status = _base_ingest_status(run_dir, "blocked_missing_search_handoff")
    status["errors"] = [{"code": reason, "status": "blocked_missing_search_handoff"}]
    _write_json(run_dir / "ingest_status.json", status)
    return status


def _source_from_search_result(
    record: Mapping[str, Any],
    *,
    index: int,
    used_source_ids: set[str],
) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
    source_id = _unique_source_id(record["id"], used_source_ids)
    url = record["url"]
    valid_url = _is_fetchable_url(url)
    policy_decision = record["policy_decision"]
    if policy_decision not in POLICY_DECISIONS:
        raise SearchHandoffError(f"unexpected policy decision: {policy_decision}")

    fetchable = valid_url and policy_decision == "allowed"
    rejection_code: str | None = None
    if not valid_url:
        rejection_code = "invalid_url"
    elif policy_decision == "blocked":
        rejection_code = "policy_blocked"
    elif policy_decision == "manual_review":
        rejection_code = "policy_manual_review"

    local_artifact_path = f"sources/{source_id}.json"
    source = {
        "id": source_id,
        "type": _source_type(record["result_type"]),
        "url": url,
        "title": record["title"],
        "published_at": record.get("published_at"),
        "accessed_at": record["accessed_at"],
        "quality": _source_quality(record.get("raw_provider_metadata")),
        "retrieval_status": "partial" if fetchable else "failed",
        "local_artifact_path": local_artifact_path,
        "license_policy": _license_policy(policy_decision),
        "robots_policy": _robots_policy(policy_decision, record["policy_flags"]),
        "search_result_id": record["id"],
        "task_id": record["task_id"],
        "angle_id": record["angle_id"],
        "route": record["route"],
        "provider": record["provider"],
        "query": record["query"],
        "snippet": record["snippet"],
        "rank": record["rank"],
        "freshness_requirement": record["freshness_requirement"],
        "language": record["language"],
        "region": record["region"],
        "policy_decision": policy_decision,
        "policy_flags": list(record["policy_flags"]),
        "raw_provider_metadata": dict(record["raw_provider_metadata"]),
    }
    error = None
    if rejection_code is not None:
        source["ingest_status"] = f"rejected_{rejection_code}"
        source["retrieval_error"] = rejection_code
        error = {
            "code": rejection_code,
            "status": "failed",
            "record_index": index,
            "record_id": record["id"],
            "source_id": source_id,
            "url": url,
        }
    else:
        source["ingest_status"] = "queued_for_fetch"
    return source, fetchable, error


def _fetch_queue_entry(source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_id": source["id"],
        "search_result_id": source["search_result_id"],
        "url": source["url"],
        "type": source["type"],
        "title": source["title"],
        "task_id": source["task_id"],
        "angle_id": source["angle_id"],
        "route": source["route"],
        "query": source["query"],
        "policy_decision": source["policy_decision"],
        "policy_flags": list(source["policy_flags"]),
        "retrieval_status": "queued",
    }


def _budget_to_evidence(preset: str, budget: BudgetPreset) -> dict[str, Any]:
    data = asdict(budget)
    data["preset"] = preset
    data["verifier_invocations_used"] = 0
    data["model_api_calls_used"] = 0
    data["sources_selected"] = 0
    data["images_selected"] = 0
    return data


def _base_ingest_status(run_dir: Path, status: str) -> dict[str, Any]:
    run_id = run_dir.name
    try:
        evidence = _read_json(run_dir / "evidence.json")
        run_id = evidence.get("run_id", run_id)
    except SearchHandoffError:
        pass
    return {
        "schema_version": INGEST_STATUS_SCHEMA_VERSION,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "status": status,
        "created_at": _utc_now(),
    }


def _create_unique_run_dir(runs_dir: Path, created_at: str) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = created_at.replace("-", "").replace(":", "").rstrip("Z")
    run_dir = runs_dir / f"dr_{timestamp}"
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = runs_dir / f"dr_{timestamp}_{suffix}"
    run_dir.mkdir()
    return run_dir.resolve()


def _unique_source_id(result_id: str, used_source_ids: set[str]) -> str:
    base = "src_" + re.sub(r"[^A-Za-z0-9_]+", "_", result_id).strip("_")
    if base == "src_":
        base = "src_result"
    candidate = base
    suffix = 1
    while candidate in used_source_ids:
        suffix += 1
        candidate = f"{base}_{suffix}"
    used_source_ids.add(candidate)
    return candidate


def _source_type(result_type: str) -> str:
    if result_type in {"pdf", "image"}:
        return result_type
    return "web"


def _source_quality(raw_metadata: Any) -> str:
    if isinstance(raw_metadata, Mapping):
        quality = raw_metadata.get("source_quality")
        if quality in {"primary", "secondary", "blog", "forum", "unknown"}:
            return str(quality)
    return "unknown"


def _license_policy(policy_decision: str) -> str:
    if policy_decision == "allowed":
        return "allowed"
    if policy_decision == "manual_review":
        return "manual_review"
    return "restricted"


def _robots_policy(policy_decision: str, policy_flags: Sequence[Any]) -> str:
    flags = " ".join(str(flag).lower() for flag in policy_flags)
    if "robots_disallowed" in flags or "robots:disallowed" in flags:
        return "disallowed"
    if policy_decision == "allowed":
        return "allowed"
    if policy_decision == "manual_review":
        return "manual_review"
    return "unknown"


def _is_fetchable_url(url: str) -> bool:
    if not url or any(character.isspace() for character in url):
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SearchHandoffError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SearchHandoffError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SearchHandoffError(f"expected JSON object in {path}")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
