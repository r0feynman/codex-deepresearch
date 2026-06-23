"""Deterministic MVP smoke suite for the Codex DeepResearch runner."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evidence_schema import EVIDENCE_SCHEMA_VERSION, validate_artifacts
from .fetch_claims import fetch_claims
from .guardrails import enforce_guardrails
from .report_generation import synthesize_report
from .verification_matrix import verify_claims
from .vision_adapter import ingest_vision_observations


MVP_SMOKE_SCHEMA_VERSION = "codex-deepresearch.mvp-smoke.v0"
FIXED_AT = "2026-06-22T00:00:00Z"
DEFAULT_INVOKE = "$deep-research: MVP smoke text-only fixture"
PLUGIN_NAME = "codex-deepresearch"

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PLUGIN_ROOT.parents[1]
MARKETPLACE_PATH = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
RUNNER_PATH = PLUGIN_ROOT / "scripts" / "codex-deepresearch"

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03\x02\x00\xef\xbf\xa7\xdb"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


class MvpSmokeError(ValueError):
    """Raised when an MVP smoke fixture fails."""


def run_mvp_smoke(
    *,
    runs_dir: str | Path,
    suite_id: str = "mvp-smoke",
    invoke: str = DEFAULT_INVOKE,
    clean: bool = False,
    require_codex_cli: bool = False,
) -> dict[str, Any]:
    """Run the local deterministic MVP smoke suite.

    The suite never calls live web, model, API, or VLM services. It writes small
    public-safe fixture runs under ``runs_dir / suite_id`` and records a
    machine-readable ``mvp_smoke_results.json`` status file.
    """

    root_runs_dir = Path(runs_dir)
    suite_dir = root_runs_dir / suite_id
    if suite_dir.exists():
        if not clean:
            raise MvpSmokeError(f"suite directory already exists: {suite_dir}")
        shutil.rmtree(suite_dir)
    suite_dir.mkdir(parents=True)

    results: dict[str, Any] = {
        "schema_version": MVP_SMOKE_SCHEMA_VERSION,
        "status": "running",
        "created_at": FIXED_AT,
        "suite_id": suite_id,
        "suite_dir": str(suite_dir.resolve()),
        "invocation": invoke,
        "install_update_smoke": _install_update_smoke(require_codex_cli=require_codex_cli),
        "fixtures": {
            "text_only": [],
            "visual_required": [],
            "visual_optional": [],
        },
        "guardrail_fixture_suite": {},
        "acceptance": {},
    }

    text_fixtures = [
        _text_fixture_from_fetch(suite_dir, "text_001_deep_research_invocation", invoke=invoke),
        _text_fixture_from_claim(suite_dir, "text_002_schema_validation"),
        _text_fixture_from_claim(suite_dir, "text_003_guardrail_clean"),
    ]
    visual_required_fixtures = [
        _visual_fixture(suite_dir, "visual_required_001_handoff", route="visual_required"),
        _visual_fixture(suite_dir, "visual_required_002_verifier", route="visual_required"),
        _visual_fixture(suite_dir, "visual_required_003_schema", route="visual_required"),
    ]
    visual_optional_fixtures = [
        _visual_fixture(suite_dir, "visual_optional_001_visual_used", route="visual_optional"),
        _visual_optional_budget_pruned_fixture(suite_dir, "visual_optional_002_budget_pruned"),
    ]
    guardrail_suite = _run_guardrail_fixture_suite(suite_dir)

    results["fixtures"]["text_only"] = text_fixtures
    results["fixtures"]["visual_required"] = visual_required_fixtures
    results["fixtures"]["visual_optional"] = visual_optional_fixtures
    results["guardrail_fixture_suite"] = guardrail_suite
    results["acceptance"] = _acceptance_summary(
        results["install_update_smoke"],
        text_fixtures=text_fixtures,
        visual_required_fixtures=visual_required_fixtures,
        visual_optional_fixtures=visual_optional_fixtures,
        guardrail_suite=guardrail_suite,
    )
    results["totals"] = {
        "text_only": len(text_fixtures),
        "visual_required": len(visual_required_fixtures),
        "visual_optional": len(visual_optional_fixtures),
        "guardrail": guardrail_suite["cases_passed"],
        "run_artifacts": len(text_fixtures) + len(visual_required_fixtures) + len(visual_optional_fixtures),
    }
    results["status"] = (
        "passed"
        if all(results["acceptance"].values())
        and results["install_update_smoke"]["status"] == "passed"
        and guardrail_suite["status"] == "passed"
        else "failed"
    )
    results_path = suite_dir / "mvp_smoke_results.json"
    results["artifacts"] = {"results": str(results_path.resolve())}
    _write_json(results_path, results)
    if results["status"] != "passed":
        raise MvpSmokeError(f"MVP smoke suite failed; see {results_path}")
    return results


def _install_update_smoke(*, require_codex_cli: bool) -> dict[str, Any]:
    manifest = _read_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")
    marketplace = _read_json(MARKETPLACE_PATH)
    skill_path = PLUGIN_ROOT / "skills" / "deep-research" / "SKILL.md"
    scripts_readme = (PLUGIN_ROOT / "scripts" / "README.md").read_text(encoding="utf-8")
    entries = marketplace.get("plugins", [])
    matches = [entry for entry in entries if isinstance(entry, Mapping) and entry.get("name") == PLUGIN_NAME]
    checks = {
        "manifest_name": manifest.get("name") == PLUGIN_NAME,
        "manifest_skills": manifest.get("skills") == "./skills/",
        "canonical_skill_exists": skill_path.is_file(),
        "runner_executable": RUNNER_PATH.is_file() and _is_executable(RUNNER_PATH),
        "marketplace_entry": len(matches) == 1,
        "install_update_docs": "Install" in scripts_readme and "Update" in scripts_readme,
    }
    if matches:
        source = matches[0].get("source", {})
        policy = matches[0].get("policy", {})
        checks["marketplace_source"] = source.get("source") == "local"
        checks["marketplace_path"] = source.get("path") == "./plugins/codex-deepresearch"
        checks["marketplace_installable"] = policy.get("installation") == "AVAILABLE"
    if require_codex_cli:
        checks["codex_cli_available"] = shutil.which("codex") is not None
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "external_network_call": False,
    }


def _text_fixture_from_fetch(suite_dir: Path, fixture_id: str, *, invoke: str) -> dict[str, Any]:
    run_dir = _create_run_dir(suite_dir, fixture_id)
    source = _source(fixture_id, route="text_only", local_artifact_path=f"sources/{fixture_id}.json")
    source_html = run_dir / "sources" / "input" / f"{fixture_id}.html"
    source_html.parent.mkdir(parents=True, exist_ok=True)
    source_html.write_text(
        "<html><head><title>MVP Text Smoke</title></head><body>"
        "<p>The DeepResearch MVP text-only invocation produces a local evidence bundle.</p>"
        "<p>The deterministic smoke suite avoids live web, model, API, and VLM calls.</p>"
        "</body></html>\n",
        encoding="utf-8",
    )
    source["url"] = source_html.resolve().as_uri()
    evidence = _base_evidence(
        run_id=fixture_id,
        question="MVP text-only invocation smoke",
        route="text_only",
        sources=[source],
        claims=[],
        extra={"invocation": invoke},
    )
    _write_json(run_dir / "evidence.json", evidence)
    _write_json(run_dir / source["local_artifact_path"], source)
    _write_json(
        run_dir / "fetch_queue.json",
        {
            "schema_version": "codex-deepresearch.fetch-queue.v0",
            "run_id": fixture_id,
            "created_at": FIXED_AT,
            "entries": [
                {
                    "source_id": source["id"],
                    "url": source["url"],
                    "type": "web",
                    "title": source["title"],
                    "task_id": "task_search_001",
                    "angle_id": "angle_001",
                    "route": "text_only",
                    "query": "MVP text-only invocation smoke",
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "retrieval_status": "queued",
                }
            ],
        },
    )
    (run_dir / "visual_observations.jsonl").write_text("", encoding="utf-8")

    fetch_status = fetch_claims(run=run_dir, timeout_seconds=5)
    _require(fetch_status["status"] == "completed", fixture_id, "fetch_claims")
    return _complete_standard_fixture(run_dir, fixture_id, "text_only")


def _text_fixture_from_claim(suite_dir: Path, fixture_id: str) -> dict[str, Any]:
    run_dir = _create_run_dir(suite_dir, fixture_id)
    source = _source(fixture_id, route="text_only")
    claim = _claim(fixture_id, route="text_only", claim_type="text", source_id=source["id"])
    _write_json(
        run_dir / "evidence.json",
        _base_evidence(
            run_id=fixture_id,
            question=f"MVP text-only fixture {fixture_id}",
            route="text_only",
            sources=[source],
            claims=[claim],
        ),
    )
    _write_json(run_dir / source["local_artifact_path"], source)
    (run_dir / "visual_observations.jsonl").write_text("", encoding="utf-8")
    return _complete_standard_fixture(run_dir, fixture_id, "text_only")


def _visual_fixture(suite_dir: Path, fixture_id: str, *, route: str) -> dict[str, Any]:
    run_dir = _create_run_dir(suite_dir, fixture_id)
    source = _source(fixture_id, route=route, source_type="image")
    _write_png(run_dir / "images" / f"{fixture_id}.png")
    _write_json(
        run_dir / "evidence.json",
        _base_evidence(
            run_id=fixture_id,
            question=f"MVP {route} fixture {fixture_id}",
            route=route,
            sources=[source],
            claims=[],
        ),
    )
    _write_json(run_dir / source["local_artifact_path"], source)
    observations_path = run_dir / "visual_handoff.jsonl"
    _write_jsonl(
        observations_path,
        [
            {
                "image_id": f"img_{fixture_id}",
                "source_id": source["id"],
                "origin": "screenshot",
                "local_artifact_path": f"images/{fixture_id}.png",
                "mime_type": "image/png",
                "width": 1,
                "height": 1,
                "observations": [f"The {fixture_id} fixture contains an inspectable local PNG."],
                "inferences": [f"The local image supports the {route} MVP visual claim."],
                "visual_tasks": ["image_claim_alignment"],
                "policy_flags": [],
            }
        ],
    )
    vision_status = ingest_vision_observations(
        run=run_dir,
        provider="codex-interactive",
        observations=observations_path,
    )
    _require(vision_status["status"] == "visual_evidence_ingested", fixture_id, "ingest_vision")
    evidence = _read_json(run_dir / "evidence.json")
    evidence["claims"] = [
        _claim(
            fixture_id,
            route=route,
            claim_type="mixed",
            source_id=source["id"],
            image_id=f"img_{fixture_id}",
        )
    ]
    _write_json(run_dir / "evidence.json", evidence)

    summary = _complete_standard_fixture(run_dir, fixture_id, route)
    summary["vision_adapter_status"] = vision_status["status"]
    summary["visual_handoff"] = True
    return summary


def _visual_optional_budget_pruned_fixture(suite_dir: Path, fixture_id: str) -> dict[str, Any]:
    run_dir = _create_run_dir(suite_dir, fixture_id)
    source = _source(fixture_id, route="visual_optional")
    claim = _claim(fixture_id, route="visual_optional", claim_type="text", source_id=source["id"])
    claim["verification_status"] = "budget_pruned"
    claim["budget_pruned"] = True
    claim["caveats"] = ["Visual optional branch was pruned by the deterministic smoke budget."]
    _write_json(
        run_dir / "evidence.json",
        _base_evidence(
            run_id=fixture_id,
            question=f"MVP visual optional budget-pruned fixture {fixture_id}",
            route="visual_optional",
            sources=[source],
            claims=[claim],
        ),
    )
    _write_json(run_dir / source["local_artifact_path"], source)
    (run_dir / "visual_observations.jsonl").write_text("", encoding="utf-8")
    summary = _complete_standard_fixture(run_dir, fixture_id, "visual_optional")
    summary["optional_visual_mode"] = "budget_pruned_no_visual"
    return summary


def _complete_standard_fixture(run_dir: Path, fixture_id: str, route: str) -> dict[str, Any]:
    guardrail_status = enforce_guardrails(run=run_dir)
    _require(guardrail_status["status"] == "completed", fixture_id, "enforce_guardrails")
    verify_status = verify_claims(run=run_dir)
    _require(verify_status["status"] == "completed", fixture_id, "verify_claims")
    report_status = synthesize_report(run=run_dir)
    _require(report_status["status"] == "completed", fixture_id, "synthesize")
    validation = _validate_fixture(run_dir)
    evidence = _read_json(run_dir / "evidence.json")
    votes = _read_jsonl(run_dir / "verifier_votes.jsonl")
    visual_observations = _read_jsonl(run_dir / "visual_observations.jsonl")
    claims = [claim for claim in evidence.get("claims", []) if isinstance(claim, Mapping)]
    return {
        "id": fixture_id,
        "route": route,
        "run_dir": str(run_dir.resolve()),
        "status": "passed",
        "evidence_valid": validation["valid"],
        "claims": len(claims),
        "claims_supported": len([claim for claim in claims if claim.get("verification_status") == "supported"]),
        "images": len(evidence.get("images", [])) if isinstance(evidence.get("images"), list) else 0,
        "visual_observations": len(visual_observations),
        "verifier_votes": len(votes),
        "visual_verifier_votes": len([vote for vote in votes if vote.get("verifier_type") == "visual"]),
        "external_model_call": False,
        "external_vlm_call": False,
        "artifacts": {
            "evidence": str((run_dir / "evidence.json").resolve()),
            "report": str((run_dir / "report.md").resolve()),
            "results": str((run_dir / "report_status.json").resolve()),
        },
    }


def _run_guardrail_fixture_suite(suite_dir: Path) -> dict[str, Any]:
    run_dir = _create_run_dir(suite_dir, "guardrail_001_policy_block")
    source = _source("guardrail_001_policy_block", route="text_only")
    source["robots_policy"] = "disallowed"
    claim = _claim("guardrail_001_policy_block", route="text_only", claim_type="text", source_id=source["id"])
    claim["review_status"] = "human_accepted"
    claim["promotion_status"] = "promoted_memory"
    claim["confidence"] = "high"
    _write_json(
        run_dir / "evidence.json",
        _base_evidence(
            run_id="guardrail_001_policy_block",
            question="MVP guardrail fixture suite",
            route="text_only",
            sources=[source],
            claims=[claim],
        ),
    )
    _write_json(run_dir / source["local_artifact_path"], source)
    status = enforce_guardrails(run=run_dir)
    validation = _validate_fixture(run_dir, include_verifier_votes=False, include_visual_observations=False)
    blocked = status.get("claims", [{}])[0].get("verification_status") == "policy_blocked"
    return {
        "status": "passed" if status["status"] == "completed" and blocked and validation["valid"] else "failed",
        "cases_total": 1,
        "cases_passed": 1 if blocked and validation["valid"] else 0,
        "run_dir": str(run_dir.resolve()),
        "evidence_valid": validation["valid"],
        "policy_blocked_claim": blocked,
        "external_model_call": False,
        "external_network_call": False,
    }


def _acceptance_summary(
    install_update_smoke: Mapping[str, Any],
    *,
    text_fixtures: Sequence[Mapping[str, Any]],
    visual_required_fixtures: Sequence[Mapping[str, Any]],
    visual_optional_fixtures: Sequence[Mapping[str, Any]],
    guardrail_suite: Mapping[str, Any],
) -> dict[str, bool]:
    text_invocation = text_fixtures[0] if text_fixtures else {}
    all_fixtures = [*text_fixtures, *visual_required_fixtures, *visual_optional_fixtures]
    return {
        "deep_research_invocation_completes_text_only_run": (
            text_invocation.get("status") == "passed"
            and Path(str(text_invocation.get("artifacts", {}).get("evidence", ""))).is_file()
            and Path(str(text_invocation.get("artifacts", {}).get("report", ""))).is_file()
        ),
        "plugin_install_update_smoke_passes": install_update_smoke.get("status") == "passed",
        "text_only_zero_vlm_calls": all(
            item.get("route") == "text_only"
            and item.get("images") == 0
            and item.get("visual_observations") == 0
            and item.get("external_vlm_call") is False
            for item in text_fixtures
        ),
        "visual_required_handoff_and_visual_verifier": all(
            item.get("visual_handoff") is True and item.get("visual_verifier_votes", 0) >= 1
            for item in visual_required_fixtures
        ),
        "evidence_validates_schema_v0": all(item.get("evidence_valid") is True for item in all_fixtures),
        "guardrail_fixture_suite_passes": guardrail_suite.get("status") == "passed",
        "fixture_counts_match_mvp_gate": (
            len(text_fixtures) == 3
            and len(visual_required_fixtures) == 3
            and len(visual_optional_fixtures) == 2
        ),
        "visual_optional_budget_pruned_path": any(
            item.get("optional_visual_mode") == "budget_pruned_no_visual"
            for item in visual_optional_fixtures
        ),
    }


def _base_evidence(
    *,
    run_id: str,
    question: str,
    route: str,
    sources: Sequence[Mapping[str, Any]],
    claims: Sequence[Mapping[str, Any]],
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": FIXED_AT,
        "question": question,
        "mode": "codex-plugin",
        "search_provider": "codex-native",
        "vlm_provider": "codex-interactive",
        "routing": [
            {
                "id": "angle_001",
                "angle": "MVP smoke fixture",
                "modality": route,
                "reason": "deterministic MVP smoke route",
                "visual_tasks": ["image_claim_alignment"] if route != "text_only" else [],
                "max_images": 1 if route != "text_only" else 0,
            }
        ],
        "search_tasks": [
            {
                "id": "task_search_001",
                "angle_id": "angle_001",
                "angle": "MVP smoke fixture",
                "query": question,
                "freshness_requirement": "any",
                "modality": route,
                "route": route,
                "max_results": 3,
                "source_policy": {"decision": "allowed", "flags": []},
            }
        ],
        "budget": {
            "preset": "quick",
            "max_sources": 3,
            "max_images": 1 if route != "text_only" else 0,
            "max_verifier_invocations": 4,
            "max_model_api_calls": 0,
            "verifier_invocations_used": 0,
            "model_api_calls_used": 0,
            "sources_selected": len(sources),
            "images_selected": 0,
        },
        "sources": [dict(source) for source in sources],
        "images": [],
        "claims": [dict(claim) for claim in claims],
        "handoff": {
            "schema_version": "codex-deepresearch.search-handoff.v0",
            "status": "fixture_loaded",
            "search_results_path": "search_results.jsonl",
            "visual_observations_path": "visual_observations.jsonl",
        },
    }
    if extra:
        evidence.update(dict(extra))
    return evidence


def _source(
    fixture_id: str,
    *,
    route: str,
    source_type: str = "web",
    local_artifact_path: str | None = None,
) -> dict[str, Any]:
    source_id = f"src_{fixture_id}"
    return {
        "id": source_id,
        "type": source_type,
        "url": f"https://example.com/{fixture_id}",
        "title": f"MVP fixture source {fixture_id}",
        "published_at": None,
        "accessed_at": FIXED_AT,
        "quality": "primary",
        "retrieval_status": "fetched",
        "local_artifact_path": local_artifact_path or f"sources/{source_id}.json",
        "license_policy": "allowed",
        "robots_policy": "allowed",
        "policy_decision": "allowed",
        "policy_flags": [],
        "route": route,
        "angle_id": "angle_001",
    }


def _claim(
    fixture_id: str,
    *,
    route: str,
    claim_type: str,
    source_id: str,
    image_id: str | None = None,
) -> dict[str, Any]:
    text = f"The MVP smoke fixture {fixture_id} has deterministic local evidence."
    return {
        "id": f"claim_{fixture_id}",
        "text": text,
        "claim_type": claim_type,
        "supporting_sources": [source_id],
        "supporting_images": [image_id] if image_id else [],
        "quote_spans": [
            {
                "source_id": source_id,
                "quote": text,
                "location": "fixture statement",
            }
        ],
        "votes": [],
        "verification_status": "unverified",
        "review_status": "not_reviewed",
        "promotion_status": "not_eligible",
        "confidence": "low",
        "caveats": [],
        "angle_id": "angle_001",
        "route": route,
    }


def _validate_fixture(
    run_dir: Path,
    *,
    include_verifier_votes: bool = True,
    include_visual_observations: bool = True,
) -> dict[str, Any]:
    visual_path = run_dir / "visual_observations.jsonl"
    votes_path = run_dir / "verifier_votes.jsonl"
    result = validate_artifacts(
        evidence_path=run_dir / "evidence.json",
        visual_observations_path=visual_path if include_visual_observations and visual_path.exists() else None,
        verifier_votes_path=votes_path if include_verifier_votes and votes_path.exists() else None,
    )
    if not result.valid:
        raise MvpSmokeError(
            f"schema validation failed for {run_dir.name}: "
            + json.dumps(result.to_dict(), sort_keys=True)
        )
    return result.to_dict()


def _create_run_dir(suite_dir: Path, fixture_id: str) -> Path:
    run_dir = suite_dir / "runs" / fixture_id
    run_dir.mkdir(parents=True)
    (run_dir / "sources").mkdir()
    (run_dir / "images").mkdir()
    (run_dir / "search_results.jsonl").write_text("", encoding="utf-8")
    (run_dir / "visual_observations.jsonl").write_text("", encoding="utf-8")
    return run_dir


def _write_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG_1X1)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise MvpSmokeError(f"expected JSON object in {path}")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + ("\n" if records else ""),
        encoding="utf-8",
    )


def _require(condition: bool, fixture_id: str, stage: str) -> None:
    if not condition:
        raise MvpSmokeError(f"{fixture_id} failed stage: {stage}")


def _is_executable(path: Path) -> bool:
    return bool(path.stat().st_mode & 0o111)
