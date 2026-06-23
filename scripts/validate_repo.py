#!/usr/bin/env python3
"""Validate the public-safe Codex DeepResearch repository scaffold."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


REQUIRED_FILES = [
    "README.md",
    "AGENTS.md",
    "docs/codex-deepresearch-prd.md",
    "docs/codex-deepresearch-project-management.md",
    "docs/codex-deepresearch-project-management.html",
    ".agents/plugins/marketplace.json",
    "plugins/codex-deepresearch/.codex-plugin/plugin.json",
    "plugins/codex-deepresearch/scripts/README.md",
    "plugins/codex-deepresearch/scripts/codex-deepresearch",
    "plugins/codex-deepresearch/src/deepresearch/__init__.py",
    "plugins/codex-deepresearch/src/deepresearch/evidence_schema.py",
    "plugins/codex-deepresearch/src/deepresearch/execution_mode.py",
    "plugins/codex-deepresearch/src/deepresearch/fetch_claims.py",
    "plugins/codex-deepresearch/src/deepresearch/manual_sources.py",
    "plugins/codex-deepresearch/src/deepresearch/modality_router.py",
    "plugins/codex-deepresearch/src/deepresearch/report_generation.py",
    "plugins/codex-deepresearch/src/deepresearch/search_handoff.py",
    "plugins/codex-deepresearch/src/deepresearch/verification_matrix.py",
    "plugins/codex-deepresearch/src/deepresearch/vision_adapter.py",
    "plugins/codex-deepresearch/skills/deep-research/SKILL.md",
    "scripts/bootstrap_github.py",
    "scripts/bootstrap_project_board.py",
    "tests/fixtures/evidence_schema/valid_evidence.json",
    "tests/fixtures/evidence_schema/search_results.jsonl",
    "tests/fixtures/evidence_schema/visual_observations.jsonl",
    "tests/fixtures/evidence_schema/verifier_votes.jsonl",
    "tests/test_evidence_schema.py",
    "tests/test_execution_mode.py",
    "tests/test_fetch_claims.py",
    "tests/test_manual_sources.py",
    "tests/test_modality_router.py",
    "tests/test_report_generation.py",
    "tests/test_search_handoff.py",
    "tests/test_verification_matrix.py",
    "tests/test_vision_adapter.py",
]


FORBIDDEN_ROOT_ENTRIES = [
    ".codex",
    ".claude",
]


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def read_json(relative_path: str) -> dict:
    path = ROOT / relative_path
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"{relative_path} is not valid JSON: {exc}")


def main() -> None:
    for relative_path in REQUIRED_FILES:
        if not (ROOT / relative_path).exists():
            fail(f"missing required file: {relative_path}")

    for entry in FORBIDDEN_ROOT_ENTRIES:
        if (ROOT / entry).exists():
            fail(f"forbidden public-repo entry exists: {entry}")

    plugin = read_json("plugins/codex-deepresearch/.codex-plugin/plugin.json")
    if plugin.get("name") != "codex-deepresearch":
        fail("plugin.json name must be codex-deepresearch")
    if plugin.get("skills") != "./skills/":
        fail("plugin.json must expose ./skills/")

    runner = ROOT / "plugins/codex-deepresearch/scripts/codex-deepresearch"
    if not os.access(runner, os.X_OK):
        fail("runner script must be executable: plugins/codex-deepresearch/scripts/codex-deepresearch")
    help_result = subprocess.run(
        [str(runner), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if help_result.returncode != 0:
        fail("runner script --help must exit 0")

    config_result = subprocess.run(
        [
            str(runner),
            "resolve-config",
            "--mode",
            "codex-plugin",
            "--search-provider",
            "codex-native",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if config_result.returncode != 0:
        fail("runner resolve-config must accept codex-plugin + codex-native")
    try:
        resolved_config = json.loads(config_result.stdout)
    except json.JSONDecodeError as exc:
        fail(f"runner resolve-config must output valid JSON: {exc}")
    if resolved_config.get("mode") != "codex-plugin":
        fail("runner resolve-config did not normalize mode")
    if resolved_config.get("search_provider") != "codex-native":
        fail("runner resolve-config did not normalize search provider")

    evidence_result = subprocess.run(
        [
            str(runner),
            "validate-evidence",
            "--evidence",
            "tests/fixtures/evidence_schema/valid_evidence.json",
            "--search-results",
            "tests/fixtures/evidence_schema/search_results.jsonl",
            "--visual-observations",
            "tests/fixtures/evidence_schema/visual_observations.jsonl",
            "--verifier-votes",
            "tests/fixtures/evidence_schema/verifier_votes.jsonl",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if evidence_result.returncode != 0:
        fail("runner validate-evidence must accept valid fixture artifacts")
    try:
        evidence_validation = json.loads(evidence_result.stdout)
    except json.JSONDecodeError as exc:
        fail(f"runner validate-evidence must output valid JSON: {exc}")
    if evidence_validation.get("valid") is not True:
        fail("runner validate-evidence did not report valid fixture artifacts")

    with tempfile.TemporaryDirectory() as manual_runs_dir:
        manual_result = subprocess.run(
            [
                str(runner),
                "ingest-manual",
                "--question",
                "Manual validation",
                "--runs-dir",
                manual_runs_dir,
                "--url",
                "https://example.com/manual-source",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    if manual_result.returncode != 0:
        fail("runner ingest-manual must accept a manual URL without external search")
    try:
        manual_validation = json.loads(manual_result.stdout)
    except json.JSONDecodeError as exc:
        fail(f"runner ingest-manual must output valid JSON: {exc}")
    if manual_validation.get("status") != "manual_sources_ingested":
        fail("runner ingest-manual did not report manual_sources_ingested")
    if manual_validation.get("sources_ingested") != 1:
        fail("runner ingest-manual did not report one ingested source")
    validation = manual_validation.get("validation")
    if not isinstance(validation, dict) or validation.get("valid") is not True:
        fail("runner ingest-manual did not produce valid evidence")

    with tempfile.TemporaryDirectory() as fetch_runs_dir:
        run_dir = Path(fetch_runs_dir) / "fetch-claims-smoke"
        sources_dir = run_dir / "sources"
        run_dir.mkdir()
        sources_dir.mkdir()
        source_html = run_dir / "source.html"
        source_html.write_text(
            "<html><head><title>Fetch Smoke</title></head><body>"
            "<p>The fetch claims smoke command extracts a source linked claim.</p>"
            "</body></html>",
            encoding="utf-8",
        )
        evidence = {
            "schema_version": "0.1.0",
            "run_id": "fetch-claims-smoke",
            "created_at": "2026-06-22T00:00:00Z",
            "question": "Fetch claims smoke",
            "mode": "codex-plugin",
            "search_provider": "codex-native",
            "vlm_provider": "codex-interactive",
            "routing": [],
            "search_tasks": [],
            "sources": [
                {
                    "id": "src_fetch_smoke",
                    "type": "web",
                    "url": source_html.resolve().as_uri(),
                    "title": "Fetch Smoke Source",
                    "published_at": None,
                    "accessed_at": "2026-06-22T00:00:00Z",
                    "quality": "unknown",
                    "retrieval_status": "partial",
                    "local_artifact_path": "sources/src_fetch_smoke.json",
                    "license_policy": "allowed",
                    "robots_policy": "allowed",
                    "policy_decision": "allowed",
                    "policy_flags": [],
                }
            ],
            "images": [],
            "claims": [],
        }
        fetch_queue = {
            "schema_version": "codex-deepresearch.fetch-queue.v0",
            "run_id": "fetch-claims-smoke",
            "created_at": "2026-06-22T00:00:00Z",
            "entries": [
                {
                    "source_id": "src_fetch_smoke",
                    "url": source_html.resolve().as_uri(),
                    "type": "web",
                    "title": "Fetch Smoke Source",
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "retrieval_status": "queued",
                }
            ],
        }
        (run_dir / "evidence.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (sources_dir / "src_fetch_smoke.json").write_text(
            json.dumps(evidence["sources"][0], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (run_dir / "fetch_queue.json").write_text(
            json.dumps(fetch_queue, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        fetch_result = subprocess.run(
            [str(runner), "fetch-claims", "--run", str(run_dir)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    if fetch_result.returncode != 0:
        fail("runner fetch-claims must accept a local queued HTML source")
    try:
        fetch_validation = json.loads(fetch_result.stdout)
    except json.JSONDecodeError as exc:
        fail(f"runner fetch-claims must output valid JSON: {exc}")
    if fetch_validation.get("status") != "completed":
        fail("runner fetch-claims did not report completed")
    if fetch_validation.get("sources_fetched") != 1:
        fail("runner fetch-claims did not report one fetched source")
    if fetch_validation.get("high_confidence_claims_created") != 0:
        fail("runner fetch-claims must not create high-confidence claims")
    validation = fetch_validation.get("validation")
    if not isinstance(validation, dict) or validation.get("valid") is not True:
        fail("runner fetch-claims did not produce valid evidence")

    with tempfile.TemporaryDirectory() as verify_runs_dir:
        run_dir = Path(verify_runs_dir) / "verify-claims-smoke"
        run_dir.mkdir()
        evidence = {
            "schema_version": "0.1.0",
            "run_id": "verify-claims-smoke",
            "created_at": "2026-06-22T00:00:00Z",
            "question": "Verify claims smoke",
            "mode": "codex-plugin",
            "search_provider": "codex-native",
            "vlm_provider": "codex-interactive",
            "routing": [
                {
                    "id": "angle_001",
                    "angle": "primary source check",
                    "modality": "text_only",
                    "reason": "smoke route",
                    "visual_tasks": [],
                    "max_images": 0,
                }
            ],
            "search_tasks": [],
            "sources": [
                {
                    "id": "src_verify_smoke",
                    "type": "web",
                    "url": "https://example.com/verify",
                    "title": "Verify Smoke Source",
                    "published_at": None,
                    "accessed_at": "2026-06-22T00:00:00Z",
                    "quality": "primary",
                    "retrieval_status": "fetched",
                    "local_artifact_path": "sources/src_verify_smoke.html",
                    "license_policy": "allowed",
                    "robots_policy": "allowed",
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "route": "text_only",
                    "angle_id": "angle_001",
                }
            ],
            "images": [],
            "claims": [
                {
                    "id": "claim_verify_smoke",
                    "text": "The smoke source contains verifiable text.",
                    "claim_type": "text",
                    "supporting_sources": ["src_verify_smoke"],
                    "supporting_images": [],
                    "quote_spans": [
                        {
                            "source_id": "src_verify_smoke",
                            "quote": "The smoke source contains verifiable text.",
                            "location": "paragraph 1",
                        }
                    ],
                    "votes": [],
                    "verification_status": "unverified",
                    "review_status": "not_reviewed",
                    "promotion_status": "not_eligible",
                    "confidence": "low",
                    "caveats": [],
                }
            ],
        }
        (run_dir / "evidence.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        verify_result = subprocess.run(
            [str(runner), "verify-claims", "--run", str(run_dir)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        synthesize_result = subprocess.run(
            [str(runner), "synthesize", "--run", str(run_dir)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        synthesize_report_exists = (run_dir / "report.md").exists()
        synthesize_status_exists = (run_dir / "report_status.json").exists()
    if verify_result.returncode != 0:
        fail("runner verify-claims must apply the verifier matrix to a text claim")
    try:
        verify_validation = json.loads(verify_result.stdout)
    except json.JSONDecodeError as exc:
        fail(f"runner verify-claims must output valid JSON: {exc}")
    if verify_validation.get("status") != "completed":
        fail("runner verify-claims did not report completed")
    if verify_validation.get("votes_written") != 3:
        fail("runner verify-claims did not write the expected three verifier votes")
    validation = verify_validation.get("validation")
    if not isinstance(validation, dict) or validation.get("valid") is not True:
        fail("runner verify-claims did not produce valid evidence and verifier votes")
    if synthesize_result.returncode != 0:
        fail("runner synthesize must generate a report from verified claims")
    try:
        synthesize_validation = json.loads(synthesize_result.stdout)
    except json.JSONDecodeError as exc:
        fail(f"runner synthesize must output valid JSON: {exc}")
    if synthesize_validation.get("status") != "completed":
        fail("runner synthesize did not report completed")
    if synthesize_validation.get("claims_included") != 1:
        fail("runner synthesize did not include the verified smoke claim")
    if not synthesize_report_exists or not synthesize_status_exists:
        fail("runner synthesize did not write report.md and report_status.json")

    with tempfile.TemporaryDirectory() as vision_runs_dir:
        prepare_result = subprocess.run(
            [
                str(runner),
                "prepare",
                "Vision adapter validation",
                "--runs-dir",
                vision_runs_dir,
                "--route",
                "visual_required",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if prepare_result.returncode != 0:
            fail("runner prepare must create a visual-required run for vision smoke")
        try:
            prepared = json.loads(prepare_result.stdout)
        except json.JSONDecodeError as exc:
            fail(f"runner prepare must output valid JSON for vision smoke: {exc}")
        vision_result = subprocess.run(
            [
                str(runner),
                "ingest-vision",
                "--run",
                prepared["run_dir"],
                "--provider",
                "codex-interactive",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    if vision_result.returncode != 0:
        fail("runner ingest-vision must accept an empty visual handoff artifact")
    try:
        vision_validation = json.loads(vision_result.stdout)
    except json.JSONDecodeError as exc:
        fail(f"runner ingest-vision must output valid JSON: {exc}")
    if vision_validation.get("status") != "needs_visual_evidence":
        fail("runner ingest-vision did not report needs_visual_evidence for missing visual results")
    validation = vision_validation.get("validation")
    if not isinstance(validation, dict) or validation.get("valid") is not True:
        fail("runner ingest-vision did not produce valid evidence")

    marketplace = read_json(".agents/plugins/marketplace.json")
    entries = marketplace.get("plugins", [])
    matching = [entry for entry in entries if entry.get("name") == "codex-deepresearch"]
    if len(matching) != 1:
        fail("marketplace must contain exactly one codex-deepresearch entry")
    source = matching[0].get("source", {})
    if source.get("source") != "local":
        fail("marketplace source.source must be local")
    if source.get("path") != "./plugins/codex-deepresearch":
        fail("marketplace source.path must be ./plugins/codex-deepresearch")
    policy = matching[0].get("policy", {})
    if policy.get("installation") != "AVAILABLE":
        fail("marketplace policy.installation must be AVAILABLE")
    if policy.get("authentication") != "ON_INSTALL":
        fail("marketplace policy.authentication must be ON_INSTALL")
    if not matching[0].get("category"):
        fail("marketplace entry must include category")

    skill_text = (ROOT / "plugins/codex-deepresearch/skills/deep-research/SKILL.md").read_text(
        encoding="utf-8"
    )
    if "name: deep-research" not in skill_text:
        fail("deep-research skill frontmatter is missing")
    if "visual_required" not in skill_text:
        fail("deep-research skill must include modality routing guidance")

    scripts_readme = (ROOT / "plugins/codex-deepresearch/scripts/README.md").read_text(
        encoding="utf-8"
    )
    for required_word in ["Install", "Update", "Remove"]:
        if required_word not in scripts_readme:
            fail(f"scripts README must document {required_word.lower()} flow")

    print("Repository scaffold validation passed.")


if __name__ == "__main__":
    main()
