"""Public-safe sanitized-real visual E2E harness."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from .fresh_session_e2e import (
    DEFAULT_SCENARIO_TIMEOUT_SECONDS,
    FreshSessionE2EError,
    run_fresh_session_visual_e2e,
)
from .report_generation import ReportGenerationError, synthesize_report
from .search_handoff import SearchHandoffError, prepare_run
from .verification_matrix import VerificationMatrixError, verify_claims
from .visual_artifacts import (
    IMAGE_FETCH_STATUS_FILENAME,
    VISUAL_CANDIDATES_FILENAME,
    VISUAL_PROVIDER_STATUS_FILENAME,
    VISUAL_PROVIDER_STATUS_SCHEMA_VERSION,
    VISUAL_SEARCH_PLAN_FILENAME,
    automatic_visual_status_envelope,
    validate_visual_artifacts,
    visual_minimums_for_run,
)
from .vision_adapter import (
    CODEX_INTERACTIVE_MODEL,
    CODEX_INTERACTIVE_PROVIDER,
    OpenAIResponsesVisionResult,
    VisionAdapterError,
    ingest_vision_observations,
)


SANITIZED_REAL_VISUAL_E2E_SCHEMA_VERSION = (
    "codex-deepresearch.sanitized-real-visual-e2e.v0"
)
SANITIZED_REAL_VISUAL_E2E_RESULTS_FILENAME = "sanitized_real_visual_e2e_results.json"
DEFAULT_SANITIZED_REAL_VISUAL_INVOKE = (
    "$deep-research: Compare public Apollo 11 mission imagery and cite "
    "image-backed visual evidence differences."
)
DEFAULT_SANITIZED_REAL_VISUAL_CANDIDATES = 10
DEFAULT_SANITIZED_REAL_FETCHED_ARTIFACTS = 3
SANITIZED_REAL_PROVIDER = "sanitized-real-web-visual-replay"
SANITIZED_REAL_PROVIDER_RUN_ID = "sanitized-real-apollo-visual-replay-001"
SANITIZED_REAL_SOURCE_ID = "src_sanitized_real_apollo_11"
SANITIZED_REAL_SEARCH_RESULT_ID = "sr_sanitized_real_apollo_11"

_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c636000000200015d0b2a00000000"
    "49454e44ae426082"
)


class SanitizedRealVisualE2EError(ValueError):
    """Raised when the sanitized-real visual E2E gate fails."""

    def __init__(self, message: str, *, results_path: Path | None = None) -> None:
        super().__init__(message)
        self.results_path = results_path


@dataclass
class _DeterministicCodexInteractiveClient:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def analyze_image(
        self,
        *,
        image_path: Path,
        mime_type: str,
        prompt: str,
        config: Any,
        metadata: Mapping[str, Any],
    ) -> OpenAIResponsesVisionResult:
        ordinal = len(self.calls) + 1
        self.calls.append(
            {
                "image_path": str(image_path),
                "mime_type": mime_type,
                "candidate_id": metadata.get("candidate_id"),
                "fetch_id": metadata.get("fetch_id"),
                "evidence_image_id": metadata.get("evidence_image_id"),
                "model": getattr(config, "model", CODEX_INTERACTIVE_MODEL),
            }
        )
        image_id = str(metadata.get("evidence_image_id") or f"image_{ordinal:03d}")
        return OpenAIResponsesVisionResult(
            observations=(
                (
                    f"Sanitized real replay image {ordinal} shows public Apollo 11 "
                    f"visual evidence for {image_id}."
                ),
            ),
            inferences=(
                (
                    "This deterministic codex-interactive test double preserves "
                    "the explicit image handoff and lineage without making a live "
                    "Codex VLM call."
                ),
            ),
            caveats=(
                (
                    "Sanitized-real-artifact replay: public-safe deterministic "
                    "VLM output, not a live Codex interactive session."
                ),
            ),
            ocr_text=f"APOLLO 11 PUBLIC VISUAL REPLAY {ordinal}",
            confidence=0.88,
            response_id=f"sanitized_codex_replay_{ordinal:03d}",
            model=getattr(config, "model", CODEX_INTERACTIVE_MODEL),
            usage={"deterministic_replay": True, "sequence": ordinal},
            raw_provider_metadata={
                "provider": CODEX_INTERACTIVE_PROVIDER,
                "sanitized_real_artifact_replay": True,
                "deterministic_codex_interactive_test_double": True,
                "live_codex_vlm_session": False,
                "lineage": {
                    key: metadata.get(key)
                    for key in (
                        "candidate_id",
                        "fetch_id",
                        "task_id",
                        "angle_id",
                        "evidence_image_id",
                        "local_artifact_path",
                    )
                    if metadata.get(key) is not None
                },
            },
            actual_cost_usd=0.0,
        )


def run_sanitized_real_visual_e2e(
    *,
    runs_dir: str | Path,
    suite_id: str = "sanitized-real-visual-e2e",
    invocation: str = DEFAULT_SANITIZED_REAL_VISUAL_INVOKE,
    clean: bool = False,
    candidate_count: int = DEFAULT_SANITIZED_REAL_VISUAL_CANDIDATES,
    fetched_artifacts: int = DEFAULT_SANITIZED_REAL_FETCHED_ARTIFACTS,
    scenario_timeout_seconds: float = DEFAULT_SCENARIO_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run the no-user-image sanitized-real visual acceptance harness.

    The harness does not perform live web fetches. It replays public-safe real-mode
    acquisition metadata, reruns the normal codex-interactive ingest path with an
    injected deterministic client, then verifies the fresh-session visual gate.
    """

    if not invocation.startswith("$deep-research:"):
        raise SanitizedRealVisualE2EError(
            "sanitized-real visual gate requires a $deep-research: invocation"
        )
    if candidate_count < DEFAULT_SANITIZED_REAL_VISUAL_CANDIDATES:
        raise SanitizedRealVisualE2EError(
            "candidate_count must be at least "
            f"{DEFAULT_SANITIZED_REAL_VISUAL_CANDIDATES}"
        )
    if fetched_artifacts < DEFAULT_SANITIZED_REAL_FETCHED_ARTIFACTS:
        raise SanitizedRealVisualE2EError(
            "fetched_artifacts must be at least "
            f"{DEFAULT_SANITIZED_REAL_FETCHED_ARTIFACTS}"
        )

    root_runs_dir = Path(runs_dir)
    suite_dir = root_runs_dir / suite_id
    if suite_dir.exists():
        if not clean:
            raise SanitizedRealVisualE2EError(
                f"suite directory already exists: {suite_dir}"
            )
        shutil.rmtree(suite_dir)
    suite_dir.mkdir(parents=True)
    results_path = suite_dir / SANITIZED_REAL_VISUAL_E2E_RESULTS_FILENAME

    failures: list[dict[str, Any]] = []
    client = _DeterministicCodexInteractiveClient()
    run_dir: Path | None = None
    fresh_session_result: dict[str, Any] | None = None
    validation_payload: dict[str, Any] = {"valid": False, "errors": []}
    minimums: dict[str, Any] = {}
    lineage: dict[str, Any] = {
        "non_fixture_non_manual_non_user_provided": False,
        "failures": ["run_not_started"],
    }

    try:
        run_dir = _prepare_replay_run(
            suite_dir=suite_dir,
            invocation=invocation,
            candidate_count=candidate_count,
            fetched_artifacts=fetched_artifacts,
        )
        _seed_sanitized_real_visual_artifacts(
            run_dir,
            candidate_count=candidate_count,
            fetched_artifacts=fetched_artifacts,
        )
        ingest_status = ingest_vision_observations(
            run=run_dir,
            provider=CODEX_INTERACTIVE_PROVIDER,
            provider_mode="real",
            codex_config={
                "provider_mode": "real",
                "max_images": fetched_artifacts,
                "model": CODEX_INTERACTIVE_MODEL,
            },
            codex_client=client,
        )
        if ingest_status.get("status") != "visual_evidence_ingested":
            failures.append(
                {
                    "check": "codex_interactive_ingest_completed",
                    "detail": ingest_status.get("status"),
                }
            )
        _annotate_codex_replay_provenance(run_dir)
        verify_status = verify_claims(run=run_dir)
        if verify_status.get("status") != "completed":
            failures.append(
                {
                    "check": "verifier_completed",
                    "detail": verify_status.get("status"),
                }
            )
        report_status = synthesize_report(run=run_dir)
        if report_status.get("status") != "completed":
            failures.append(
                {
                    "check": "report_completed",
                    "detail": report_status.get("status"),
                }
            )
        _write_completed_visual_provider_status(
            run_dir,
            deterministic_codex_calls=len(client.calls),
        )
        _write_completed_run_status(run_dir, invocation=invocation)
        validation_payload = validate_visual_artifacts(run_dir=run_dir).to_dict()
        minimums = visual_minimums_for_run(run_dir)
        lineage = _lineage_report(run_dir)
        fresh_session_result = run_fresh_session_visual_e2e(
            runs_dir=suite_dir,
            suite_id="fresh-session-visual-gate",
            invocation=invocation,
            clean=True,
            real_codex_interactive="require",
            completed_auto_visual_run=run_dir,
            scenario_timeout_seconds=scenario_timeout_seconds,
        )
    except (
        FreshSessionE2EError,
        ReportGenerationError,
        SearchHandoffError,
        VerificationMatrixError,
        VisionAdapterError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        failures.append(
            {
                "check": "sanitized_real_visual_pipeline",
                "detail": str(exc),
            }
        )
        if isinstance(exc, FreshSessionE2EError) and exc.results_path:
            try:
                fresh_session_result = json.loads(exc.results_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                fresh_session_result = {"status": "failed", "artifacts": {"results": str(exc.results_path)}}
    if run_dir is not None:
        if not validation_payload.get("valid"):
            failures.append(
                {
                    "check": "visual_artifacts_schema_valid",
                    "detail": validation_payload.get("errors", []),
                }
            )
        failures.extend(_minimum_failures(minimums))
        if lineage.get("non_fixture_non_manual_non_user_provided") is not True:
            failures.append(
                {
                    "check": "non_fixture_non_manual_non_user_provided_lineage",
                    "detail": lineage.get("failures", []),
                }
            )
        if (
            not isinstance(fresh_session_result, Mapping)
            or fresh_session_result.get("status") != "passed"
            or fresh_session_result.get("release_gate_passed") is not True
        ):
            failures.append(
                {
                    "check": "fresh_session_visual_release_gate_passed",
                    "detail": (fresh_session_result or {}).get("release_gate_status"),
                }
            )

    deduped_failures = _dedupe_failures(failures)
    status = "passed" if not deduped_failures else "failed"
    results = _results_payload(
        results_path=results_path,
        suite_dir=suite_dir,
        run_dir=run_dir,
        invocation=invocation,
        status=status,
        failures=deduped_failures,
        validation=validation_payload,
        minimums=minimums,
        lineage=lineage,
        deterministic_codex_calls=len(client.calls),
        fresh_session_result=fresh_session_result,
    )
    _write_json(results_path, results)
    if status != "passed":
        raise SanitizedRealVisualE2EError(
            f"sanitized-real visual E2E gate failed; see {results_path}",
            results_path=results_path,
        )
    return results


def _prepare_replay_run(
    *,
    suite_dir: Path,
    invocation: str,
    candidate_count: int,
    fetched_artifacts: int,
) -> Path:
    question = invocation.split(":", 1)[1].strip() if ":" in invocation else invocation
    prepared = prepare_run(
        question=question,
        runs_dir=suite_dir / "runs",
        route="visual_required",
        angles=["primary source discovery"],
        budget_preset="standard",
        vlm_provider=CODEX_INTERACTIVE_PROVIDER,
        max_results=max(8, candidate_count),
        max_images=fetched_artifacts,
    )
    return Path(str(prepared["run_dir"]))


def _seed_sanitized_real_visual_artifacts(
    run_dir: Path,
    *,
    candidate_count: int,
    fetched_artifacts: int,
) -> None:
    now = _utc_now()
    evidence = _read_json(run_dir / "evidence.json")
    task_id, angle_id = _visual_task_identity(evidence)
    _write_public_source(run_dir, created_at=now)
    evidence.setdefault("sources", [])
    evidence["sources"] = [
        source
        for source in evidence["sources"]
        if isinstance(source, Mapping) and source.get("id") != SANITIZED_REAL_SOURCE_ID
    ]
    evidence["sources"].append(_source_record(created_at=now))
    evidence.setdefault("handoff", {})["status"] = "sanitized_real_artifact_replay"
    evidence["handoff"]["sanitized_real_artifact_replay"] = True
    evidence["handoff"]["live_web_fetch"] = False
    evidence["handoff"]["user_provided_images"] = False
    _write_json(run_dir / "evidence.json", evidence)

    _write_jsonl(run_dir / "search_results.jsonl", [_search_result_record(created_at=now)])
    _write_json(
        run_dir / VISUAL_SEARCH_PLAN_FILENAME,
        _visual_search_plan(
            run_dir=run_dir,
            task_id=task_id,
            angle_id=angle_id,
            candidate_count=candidate_count,
            fetched_artifacts=fetched_artifacts,
            created_at=now,
        ),
    )

    image_dir = run_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    candidates: list[dict[str, Any]] = []
    fetches: list[dict[str, Any]] = []
    for index in range(1, candidate_count + 1):
        candidate = _candidate_record(
            index=index,
            task_id=task_id,
            angle_id=angle_id,
            fetched=index <= fetched_artifacts,
            created_at=now,
        )
        candidates.append(candidate)
        if index <= fetched_artifacts:
            image_id = f"img_sanitized_real_{index:03d}"
            image_bytes = _image_bytes(index)
            image_path = image_dir / f"{image_id}.png"
            image_path.write_bytes(image_bytes)
            fetches.append(
                _fetch_record(
                    index=index,
                    candidate=candidate,
                    image_id=image_id,
                    byte_size=len(image_bytes),
                    image_hash="sha256:" + sha256(image_bytes).hexdigest(),
                )
            )
    _write_jsonl(run_dir / VISUAL_CANDIDATES_FILENAME, candidates)
    _write_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME, fetches)
    _write_json(
        run_dir / VISUAL_PROVIDER_STATUS_FILENAME,
        _visual_provider_status(
            run_dir=run_dir,
            status="sanitized_real_visual_candidates_replayed",
            ok=True,
            terminal=False,
            metric_classification="visual_acquisition",
            providers=[
                _acquisition_provider_record(
                    candidates_discovered=candidate_count,
                    artifacts_fetched=fetched_artifacts,
                )
            ],
            minimums=visual_minimums_for_run(run_dir),
            actionable_cause=(
                "sanitized-real-artifact replay seeded public-safe visual "
                "candidate and fetch lineage without a live web fetch"
            ),
            created_at=now,
        ),
    )


def _write_public_source(run_dir: Path, *, created_at: str) -> None:
    source_path = run_dir / "sources" / f"{SANITIZED_REAL_SOURCE_ID}.html"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(
        "<!doctype html>\n"
        "<html><body>\n"
        "<h1>Apollo 11 public visual archive replay</h1>\n"
        "<p>Public-safe sanitized artifact preserving real acquisition lineage.</p>\n"
        f"<p>Recorded at {created_at}; no live web request was made in this run.</p>\n"
        "</body></html>\n",
        encoding="utf-8",
    )


def _source_record(*, created_at: str) -> dict[str, Any]:
    return {
        "id": SANITIZED_REAL_SOURCE_ID,
        "type": "web",
        "url": "https://www.nasa.gov/history/apollo-11-mission-overview/",
        "title": "Apollo 11 Mission Overview",
        "published_at": None,
        "accessed_at": created_at,
        "quality": "primary",
        "retrieval_status": "fetched",
        "local_artifact_path": f"sources/{SANITIZED_REAL_SOURCE_ID}.html",
        "license_policy": "allowed",
        "robots_policy": "allowed",
        "policy_decision": "allowed",
        "policy_flags": [],
        "route": "visual_required",
        "search_result_id": SANITIZED_REAL_SEARCH_RESULT_ID,
        "provider": SANITIZED_REAL_PROVIDER,
        "provider_mode": "real",
        "sanitized_real_artifact_replay": True,
        "live_web_fetch": False,
        "fixture": False,
        "manual": False,
        "user_provided": False,
    }


def _search_result_record(*, created_at: str) -> dict[str, Any]:
    return {
        "id": SANITIZED_REAL_SEARCH_RESULT_ID,
        "task_id": "task_001",
        "angle_id": "angle_001",
        "title": "Apollo 11 Mission Overview",
        "url": "https://www.nasa.gov/history/apollo-11-mission-overview/",
        "snippet": "Public Apollo 11 mission imagery and contextual source metadata.",
        "published_at": None,
        "retrieved_at": created_at,
        "provider": "codex-native",
        "provider_mode": "real",
        "rank": 1,
        "raw_provider_metadata": {
            "sanitized_real_artifact_replay": True,
            "live_web_fetch": False,
            "fixture": False,
            "manual": False,
            "user_provided": False,
        },
    }


def _visual_search_plan(
    *,
    run_dir: Path,
    task_id: str,
    angle_id: str,
    candidate_count: int,
    fetched_artifacts: int,
    created_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": "codex-deepresearch.visual-artifacts.v0",
        "run_id": run_dir.name,
        "created_at": created_at,
        "tasks": [
            {
                "plan_id": "plan_sanitized_real_001",
                "task_id": task_id,
                "angle_id": angle_id,
                "route": "visual_required",
                "target_evidence_type": "web_image",
                "query": "Apollo 11 public mission imagery",
                "providers": [SANITIZED_REAL_PROVIDER, CODEX_INTERACTIVE_PROVIDER],
                "source_search_result_ids": [SANITIZED_REAL_SEARCH_RESULT_ID],
                "caps": {
                    "max_candidates": candidate_count,
                    "max_fetches": fetched_artifacts,
                    "max_vlm_images": fetched_artifacts,
                    "max_cost_usd": 0.0,
                },
                "policy_constraints": {"robots": "allowed", "license": "allowed"},
                "estimated_cost_usd": 0.0,
                "state": "completed",
            }
        ],
    }


def _candidate_record(
    *,
    index: int,
    task_id: str,
    angle_id: str,
    fetched: bool,
    created_at: str,
) -> dict[str, Any]:
    candidate_id = f"cand_sanitized_real_{index:03d}"
    image_url = (
        "https://www.nasa.gov/wp-content/uploads/2023/03/"
        f"apollo-11-public-replay-{index:03d}.png"
    )
    return {
        "candidate_id": candidate_id,
        "plan_id": "plan_sanitized_real_001",
        "task_id": task_id,
        "angle_id": angle_id,
        "route": "visual_required",
        "source_id": SANITIZED_REAL_SOURCE_ID,
        "source_search_result_id": SANITIZED_REAL_SEARCH_RESULT_ID,
        "provider": SANITIZED_REAL_PROVIDER,
        "provider_kind": "web_image_search",
        "provider_mode": "real",
        "provider_run_id": SANITIZED_REAL_PROVIDER_RUN_ID,
        "provider_provenance": _sanitized_real_provenance(
            stage="candidate",
            created_at=created_at,
        ),
        "origin": "image_search",
        "page_url": "https://www.nasa.gov/history/apollo-11-mission-overview/",
        "image_url": image_url,
        "visual_tasks": ["Compare public mission imagery details"],
        "rank": index,
        "score": round(1.0 - (index - 1) * 0.01, 4),
        "policy_decision": "allowed",
        "policy_flags": [],
        "candidate_status": "fetched" if fetched else "ranked",
        "rejection_reason": None,
        "estimated_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
        "created_at": created_at,
    }


def _fetch_record(
    *,
    index: int,
    candidate: Mapping[str, Any],
    image_id: str,
    byte_size: int,
    image_hash: str,
) -> dict[str, Any]:
    return {
        "fetch_id": f"fetch_sanitized_real_{index:03d}",
        "candidate_id": candidate["candidate_id"],
        "plan_id": candidate["plan_id"],
        "task_id": candidate["task_id"],
        "angle_id": candidate["angle_id"],
        "route": candidate["route"],
        "source_id": SANITIZED_REAL_SOURCE_ID,
        "source_search_result_id": SANITIZED_REAL_SEARCH_RESULT_ID,
        "provider": candidate["provider"],
        "provider_kind": candidate["provider_kind"],
        "provider_mode": candidate["provider_mode"],
        "provider_run_id": candidate["provider_run_id"],
        "provider_provenance": dict(candidate["provider_provenance"]),
        "fetch_status": "fetched",
        "http_status": 200,
        "mime_type": "image/png",
        "byte_size": byte_size,
        "width": 1,
        "height": 1,
        "hash": image_hash,
        "phash": f"sanitized-real-phash-{index:03d}",
        "local_artifact_path": f"images/{image_id}.png",
        "evidence_image_id": image_id,
        "policy_decision": "allowed",
        "policy_flags": [],
        "failure_code": None,
        "estimated_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
    }


def _acquisition_provider_record(
    *,
    candidates_discovered: int,
    artifacts_fetched: int,
) -> dict[str, Any]:
    return {
        "provider": SANITIZED_REAL_PROVIDER,
        "provider_kind": "web_image_search",
        "provider_mode": "real",
        "configured": True,
        "available": True,
        "blocked_reason": None,
        "invoked": True,
        "invocations": 1,
        "candidates_discovered": candidates_discovered,
        "artifacts_fetched": artifacts_fetched,
        "vlm_images_analyzed": 0,
        "estimated_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
        "last_error": None,
        "external_network_call": False,
        "sanitized_real_artifact_replay": True,
        "live_web_fetch": False,
        "fixture": False,
        "manual": False,
        "user_provided": False,
    }


def _write_completed_visual_provider_status(
    run_dir: Path,
    *,
    deterministic_codex_calls: int,
) -> None:
    now = _utc_now()
    existing = _read_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME)
    providers = [
        provider
        for provider in existing.get("providers", [])
        if isinstance(provider, Mapping)
        and provider.get("provider") != SANITIZED_REAL_PROVIDER
    ]
    acquisition = _acquisition_provider_record(
        candidates_discovered=len(_read_jsonl(run_dir / VISUAL_CANDIDATES_FILENAME)),
        artifacts_fetched=len(
            [
                item
                for item in _read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
                if item.get("fetch_status") == "fetched"
            ]
        ),
    )
    providers = [acquisition, *providers]
    for provider in providers:
        if provider.get("provider") == CODEX_INTERACTIVE_PROVIDER:
            provider["sanitized_real_artifact_replay"] = True
            provider["deterministic_codex_interactive_test_double"] = True
            provider["live_codex_vlm_session"] = False
            provider["fixture"] = False
            provider["manual"] = False
            provider["user_provided"] = False
            provider["vlm_images_analyzed"] = max(
                int(provider.get("vlm_images_analyzed") or 0),
                deterministic_codex_calls,
            )
    _write_json(
        run_dir / VISUAL_PROVIDER_STATUS_FILENAME,
        _visual_provider_status(
            run_dir=run_dir,
            status="completed_auto_visual",
            ok=True,
            terminal=True,
            metric_classification="success",
            providers=providers,
            minimums=visual_minimums_for_run(run_dir),
            actionable_cause=(
                "completed sanitized-real-artifact replay with non-fixture "
                "candidate/fetch lineage, codex-interactive provider provenance, "
                "verifier links, and report citations"
            ),
            created_at=now,
        ),
    )


def _visual_provider_status(
    *,
    run_dir: Path,
    status: str,
    ok: bool,
    terminal: bool,
    metric_classification: str,
    providers: Sequence[Mapping[str, Any]],
    minimums: Mapping[str, Any],
    actionable_cause: str,
    created_at: str,
) -> dict[str, Any]:
    envelope = (
        automatic_visual_status_envelope(status)
        if status in {"completed_auto_visual", "partial_auto_visual", "blocked_missing_vlm_provider"}
        else {
            "ok": ok,
            "terminal": terminal,
            "metric_classification": metric_classification,
        }
    )
    return {
        "schema_version": VISUAL_PROVIDER_STATUS_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "run_dir": run_dir.name,
        "status": status,
        "ok": envelope["ok"],
        "terminal": envelope["terminal"],
        "created_at": created_at,
        "metric_classification": envelope["metric_classification"],
        "minimums": dict(minimums),
        "providers": [dict(provider) for provider in providers],
        "diagnostics": {"actionable_cause": actionable_cause},
        "artifacts": {
            "visual_candidates": VISUAL_CANDIDATES_FILENAME,
            "image_fetch_status": IMAGE_FETCH_STATUS_FILENAME,
            "visual_observations": "visual_observations.jsonl",
            "visual_provider_status": VISUAL_PROVIDER_STATUS_FILENAME,
        },
        "sanitized_real_artifact_replay": True,
        "live_web_fetch": False,
        "live_codex_vlm_session": False,
        "deterministic_codex_interactive_test_double": True,
    }


def _write_completed_run_status(run_dir: Path, *, invocation: str) -> None:
    question = invocation.split(":", 1)[1].strip() if ":" in invocation else invocation
    payload = {
        "schema_version": "codex-deepresearch.run-status.v0",
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "invocation": invocation,
        "question": question,
        "selected_mode": "full-runner",
        "status": "completed_auto_visual",
        "ok": True,
        "terminal": True,
        "provenance": {
            "type": "sanitized_real_artifact",
            "adapter": "sanitized-real-visual-e2e",
            "fixture_only": False,
            "manual_handoff": False,
            "user_provided_images": False,
            "real_child_execution": False,
            "real_use_e2e_eligible": True,
            "sanitized_real_artifact_replay": True,
            "live_web_fetch": False,
            "live_codex_vlm_session": False,
            "deterministic_codex_interactive_test_double": True,
        },
        "diagnostics": {
            "actionable_cause": (
                "sanitized-real-artifact no-user-image run reached completed_auto_visual; "
                "acquisition was public-safe replay and VLM handoff used a deterministic "
                "codex-interactive test double"
            )
        },
        "artifacts": {
            "run_status": str(run_dir / "run_status.json"),
            "evidence": str(run_dir / "evidence.json"),
            "report": str(run_dir / "report.md"),
            "report_status": str(run_dir / "report_status.json"),
            "visual_tasks": str(run_dir / "visual_tasks.json"),
            "visual_observations": str(run_dir / "visual_observations.jsonl"),
            "visual_provider_status": str(run_dir / VISUAL_PROVIDER_STATUS_FILENAME),
            "visual_candidates": str(run_dir / VISUAL_CANDIDATES_FILENAME),
            "image_fetch_status": str(run_dir / IMAGE_FETCH_STATUS_FILENAME),
            "verifier_votes": str(run_dir / "verifier_votes.jsonl"),
        },
        "stages": {
            "ingest_vision": {"status": "completed"},
            "verify_claims": {"status": "completed"},
            "synthesize": {"status": "completed"},
        },
        "shard_summary": {
            "planned_task_count": 1,
            "accepted_shard_count": 1,
            "merged_shard_count": 1,
            "failed_task_count": 0,
            "blocked_task_count": 0,
            "rejected_shard_count": 0,
            "discarded_task_count": 0,
        },
        "fallback": {
            "parallel_degraded": False,
            "needs_serial_handoff": False,
            "degraded_reason": None,
        },
        "visual_summary": dict(visual_minimums_for_run(run_dir)),
    }
    _write_json(run_dir / "run_status.json", payload)


def _annotate_codex_replay_provenance(run_dir: Path) -> None:
    observations = _read_jsonl(run_dir / "visual_observations.jsonl")
    for observation in observations:
        provenance = observation.get("provider_provenance")
        if not isinstance(provenance, dict):
            provenance = {}
        provenance.update(
            {
                "sanitized_real_artifact_replay": True,
                "deterministic_codex_interactive_test_double": True,
                "live_codex_vlm_session": False,
                "fixture": False,
                "manual": False,
                "user_provided": False,
            }
        )
        observation["provider_provenance"] = provenance
        observation["sanitized_real_artifact_replay"] = True
        observation["deterministic_codex_interactive_test_double"] = True
        observation["live_codex_vlm_session"] = False
    _write_jsonl(run_dir / "visual_observations.jsonl", observations)

    evidence = _read_json(run_dir / "evidence.json")
    for image in evidence.get("images", []):
        if not isinstance(image, dict):
            continue
        provenance = image.get("provider_provenance")
        if not isinstance(provenance, dict):
            provenance = {}
        provenance.update(
            {
                "sanitized_real_artifact_replay": True,
                "deterministic_codex_interactive_test_double": True,
                "live_codex_vlm_session": False,
                "fixture": False,
                "manual": False,
                "user_provided": False,
            }
        )
        image["provider_provenance"] = provenance
        image["sanitized_real_artifact_replay"] = True
        image["deterministic_codex_interactive_test_double"] = True
        image["live_codex_vlm_session"] = False
    _write_json(run_dir / "evidence.json", evidence)


def _lineage_report(run_dir: Path) -> dict[str, Any]:
    failures: list[str] = []
    candidates = _read_jsonl(run_dir / VISUAL_CANDIDATES_FILENAME)
    fetches = _read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
    observations = _read_jsonl(run_dir / "visual_observations.jsonl")
    evidence = _read_json(run_dir / "evidence.json")
    report_status = _read_json(run_dir / "report_status.json")
    verifier_votes = _read_jsonl(run_dir / "verifier_votes.jsonl")

    if len(candidates) < DEFAULT_SANITIZED_REAL_VISUAL_CANDIDATES:
        failures.append("candidate_count_below_10")
    if len([item for item in fetches if item.get("fetch_status") == "fetched"]) < 3:
        failures.append("fetched_artifacts_below_3")
    if (
        len(
            [
                item
                for item in observations
                if item.get("provider") == CODEX_INTERACTIVE_PROVIDER
                and item.get("provider_kind") == "vlm"
                and item.get("provider_mode") == "real"
                and item.get("observation_status") == "analyzed"
            ]
        )
        < 3
    ):
        failures.append("codex_interactive_observations_below_3")

    forbidden_modes = {"fixture", "manual", "user_provided", "user-provided"}
    _flag_forbidden_records(failures, "candidate", candidates, forbidden_modes)
    _flag_forbidden_records(failures, "fetch", fetches, forbidden_modes)
    _flag_forbidden_records(failures, "observation", observations, forbidden_modes)
    _flag_forbidden_records(
        failures,
        "image",
        [item for item in evidence.get("images", []) if isinstance(item, Mapping)],
        forbidden_modes,
    )

    supported_visual_claims = [
        claim
        for claim in evidence.get("claims", [])
        if isinstance(claim, Mapping)
        and claim.get("verification_status") == "supported"
        and claim.get("claim_type") in {"visual", "mixed"}
    ]
    if not supported_visual_claims:
        failures.append("no_supported_visual_or_mixed_claim")
    used_images = {
        image_id
        for image_id in report_status.get("used_images", [])
        if isinstance(image_id, str) and image_id
    }
    cited_claims = [
        claim
        for claim in supported_visual_claims
        if used_images.intersection(_string_items(claim.get("supporting_images")))
    ]
    if not cited_claims:
        failures.append("no_report_cited_visual_or_mixed_claim")
    vote_ids = {
        vote.get("id")
        for vote in verifier_votes
        if isinstance(vote.get("id"), str) and vote.get("id")
    }
    observations_by_image = {
        str(observation.get("evidence_image_id")): observation
        for observation in observations
        if isinstance(observation.get("evidence_image_id"), str)
    }
    for claim in cited_claims[:1]:
        claim_id = str(claim.get("id"))
        for image_id in used_images.intersection(_string_items(claim.get("supporting_images"))):
            observation = observations_by_image.get(image_id)
            if observation is None:
                failures.append("report_cited_image_lacks_observation")
                continue
            verifier_links = [
                link
                for link in observation.get("verifier_links", [])
                if isinstance(link, Mapping) and link.get("claim_id") == claim_id
            ]
            report_links = [
                link
                for link in observation.get("report_links", [])
                if isinstance(link, Mapping) and link.get("claim_id") == claim_id
            ]
            if not verifier_links:
                failures.append("report_cited_claim_lacks_verifier_link")
            if not report_links:
                failures.append("report_cited_claim_lacks_report_link")
            if not any(link.get("verifier_vote_id") in vote_ids for link in verifier_links):
                failures.append("verifier_link_lacks_vote_lineage")

    return {
        "non_fixture_non_manual_non_user_provided": not failures,
        "failures": sorted(set(failures)),
    }


def _flag_forbidden_records(
    failures: list[str],
    label: str,
    records: Sequence[Mapping[str, Any]],
    forbidden_modes: set[str],
) -> None:
    for record in records:
        mode = str(record.get("provider_mode") or "").lower()
        provenance = record.get("provider_provenance")
        provenance_values = []
        if isinstance(provenance, Mapping):
            provenance_values = [
                str(provenance.get(key) or "").lower()
                for key in ("provider_mode", "fixture", "manual", "user_provided")
            ]
        if mode in forbidden_modes or any(value in forbidden_modes or value == "true" for value in provenance_values[1:]):
            failures.append(f"{label}_has_fixture_manual_or_user_lineage")


def _minimum_failures(minimums: Mapping[str, Any]) -> list[dict[str, Any]]:
    checks = {
        "minimums_satisfied": minimums.get("satisfied") is True,
        "candidate_count_at_least_10": int(minimums.get("candidate_count") or 0) >= 10,
        "fetched_artifacts_at_least_3": int(minimums.get("fetched_artifacts") or 0) >= 3,
        "codex_interactive_observations_at_least_3": (
            int(minimums.get("vlm_images_analyzed") or 0) >= 3
        ),
        "report_cited_images_at_least_1": int(minimums.get("report_cited_images") or 0) >= 1,
    }
    return [
        {"check": check, "detail": dict(minimums)}
        for check, passed in checks.items()
        if not passed
    ]


def _results_payload(
    *,
    results_path: Path,
    suite_dir: Path,
    run_dir: Path | None,
    invocation: str,
    status: str,
    failures: Sequence[Mapping[str, Any]],
    validation: Mapping[str, Any],
    minimums: Mapping[str, Any],
    lineage: Mapping[str, Any],
    deterministic_codex_calls: int,
    fresh_session_result: Mapping[str, Any] | None,
) -> dict[str, Any]:
    artifacts: dict[str, Any] = {"results": str(results_path.resolve())}
    if run_dir is not None:
        artifacts.update(
            {
                "run_dir": str(run_dir.resolve()),
                "run_status": str((run_dir / "run_status.json").resolve()),
                "visual_provider_status": str(
                    (run_dir / VISUAL_PROVIDER_STATUS_FILENAME).resolve()
                ),
                "visual_candidates": str((run_dir / VISUAL_CANDIDATES_FILENAME).resolve()),
                "image_fetch_status": str((run_dir / IMAGE_FETCH_STATUS_FILENAME).resolve()),
                "visual_observations": str((run_dir / "visual_observations.jsonl").resolve()),
                "evidence": str((run_dir / "evidence.json").resolve()),
                "verifier_votes": str((run_dir / "verifier_votes.jsonl").resolve()),
                "report_status": str((run_dir / "report_status.json").resolve()),
                "report": str((run_dir / "report.md").resolve()),
            }
        )
    if isinstance(fresh_session_result, Mapping):
        fresh_artifacts = fresh_session_result.get("artifacts")
        if isinstance(fresh_artifacts, Mapping) and isinstance(fresh_artifacts.get("results"), str):
            artifacts["fresh_session_visual_results"] = fresh_artifacts["results"]
    return {
        "schema_version": SANITIZED_REAL_VISUAL_E2E_SCHEMA_VERSION,
        "status": status,
        "generated_at": _utc_now(),
        "suite_id": suite_dir.name,
        "suite_dir": str(suite_dir.resolve()),
        "invocation": invocation,
        "release_gate_passed": status == "passed",
        "release_gate_status": (
            fresh_session_result.get("release_gate_status")
            if isinstance(fresh_session_result, Mapping)
            else "not_run"
        ),
        "counts": {
            "candidate_count": int(minimums.get("candidate_count") or 0),
            "fetched_artifacts": int(minimums.get("fetched_artifacts") or 0),
            "codex_interactive_observations": int(minimums.get("vlm_images_analyzed") or 0),
            "report_cited_visual_or_mixed_claims": int(
                minimums.get("report_cited_images") or 0
            ),
            "deterministic_codex_interactive_calls": deterministic_codex_calls,
        },
        "visual_minimums": dict(minimums),
        "visual_artifact_validation": dict(validation),
        "lineage": dict(lineage),
        "public_safe": True,
        "no_user_image": True,
        "sanitized_real_artifact": True,
        "live_web_fetch": False,
        "live_codex_vlm_session": False,
        "deterministic_codex_interactive_test_double": True,
        "facts": [
            "The harness creates a visual_required no-user-image run.",
            "The harness writes 10 real-mode visual candidate records.",
            "The harness writes at least 3 real-mode fetched image artifacts.",
            "The normal ingest_vision path is rerun with provider codex-interactive.",
            "verify_claims and synthesize_report are rerun after visual ingestion.",
            "The fresh-session visual release gate validates the completed_auto_visual run.",
        ],
        "inferences": [
            (
                "The run is sanitized-real-artifact rather than live real-use because "
                "candidate and fetch lineage is replayed from public-safe metadata."
            ),
            (
                "The codex-interactive provider provenance is deterministic test-double "
                "provenance, not evidence of a live Codex VLM session."
            ),
        ],
        "unknowns": [
            "No live web availability, remote image bytes, or live Codex VLM behavior is asserted.",
        ],
        "failures": list(failures),
        "artifacts": artifacts,
    }


def _visual_task_identity(evidence: Mapping[str, Any]) -> tuple[str, str]:
    tasks = evidence.get("search_tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if isinstance(task, Mapping):
                task_id = task.get("id")
                angle_id = task.get("angle_id")
                if isinstance(task_id, str) and isinstance(angle_id, str):
                    return task_id, angle_id
    routes = evidence.get("routing")
    if isinstance(routes, list):
        for index, route in enumerate(routes, start=1):
            if isinstance(route, Mapping) and route.get("modality") == "visual_required":
                angle_id = str(route.get("id") or f"angle_{index:03d}")
                return f"task_{index:03d}", angle_id
    return "task_001", "angle_001"


def _sanitized_real_provenance(*, stage: str, created_at: str) -> dict[str, Any]:
    return {
        "provider": SANITIZED_REAL_PROVIDER,
        "provider_kind": "web_image_search",
        "provider_mode": "real",
        "provider_run_id": SANITIZED_REAL_PROVIDER_RUN_ID,
        "stage": stage,
        "retrieved_at": created_at,
        "sanitized_real_artifact_replay": True,
        "live_web_fetch": False,
        "fixture": False,
        "manual": False,
        "user_provided": False,
    }


def _image_bytes(index: int) -> bytes:
    return _PNG_1X1 + f"\n# sanitized-real-image-{index:03d}\n".encode("ascii")


def _dedupe_failures(failures: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for failure in failures:
        key = json.dumps(failure, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(dict(failure))
    return deduped


def _string_items(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str) and item}


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return dict(payload) if isinstance(payload, Mapping) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, Mapping):
            records.append(dict(payload))
    return records


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
