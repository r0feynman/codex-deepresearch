from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "plugins" / "codex-deepresearch" / "scripts" / "codex-deepresearch"
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch.automated_visual_e2e import (  # noqa: E402
    AutomatedVisualE2EError,
    evaluate_automated_visual_run,
    run_automated_visual_e2e,
)


SCENARIOS = {
    "product_image_discovery": ("web_image_search", "image_search", "web_image"),
    "ui_screenshot_comparison": ("screenshot", "screenshot", "screenshot"),
    "public_chart_report_visual_extraction": ("page_extractor", "page_image", "chart_image"),
    "public_pdf_paper_figure_extraction": ("pdf_rasterizer", "pdf_figure", "pdf_figure"),
}


class AutomatedVisualE2ETests(unittest.TestCase):
    def temp_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def read_json(self, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def write_jsonl(self, path: Path, records: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
            encoding="utf-8",
        )

    def test_gate_passes_four_real_scenario_runs_and_counts_release_metrics(self) -> None:
        runs_dir = self.temp_dir()
        scenario_runs = {
            scenario_id: self.write_real_scenario_run(
                runs_dir / scenario_id,
                scenario_id=scenario_id,
            )
            for scenario_id in SCENARIOS
        }

        result = run_automated_visual_e2e(
            runs_dir=self.temp_dir(),
            suite_id="visual-gate",
            clean=True,
            scenario_runs=scenario_runs,
        )

        self.assertEqual(result["status"], "passed")
        self.assertTrue(all(result["acceptance"].values()), result["acceptance"])
        self.assertEqual(result["outcome_counts"], {"blocked": 0, "failed": 0, "passed": 4})
        product = {
            scenario["id"]: scenario for scenario in result["scenarios"]
        }["product_image_discovery"]
        self.assertEqual(product["counts"]["scenario_real_candidates"], 10)
        for scenario in result["scenarios"]:
            self.assertEqual(
                scenario["counts"]["real_openai_responses_vision_observations"],
                3,
            )
            self.assertEqual(scenario["counts"]["report_cited_visual_or_mixed_claims"], 1)
            self.assertEqual(
                scenario["release_numerator_counts"]["real_vlm_images_analyzed"],
                3,
            )

    def test_missing_real_runs_block_with_structured_provider_diagnostics(self) -> None:
        with self.assertRaises(AutomatedVisualE2EError) as raised:
            run_automated_visual_e2e(
                runs_dir=self.temp_dir(),
                suite_id="blocked-gate",
                clean=True,
            )

        payload = self.read_json(raised.exception.results_path)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["outcome_counts"], {"blocked": 4, "failed": 0, "passed": 0})
        self.assertGreaterEqual(payload["classification_counts"]["provider"], 4)
        self.assertIn("brave_image_search", payload["preflight"])
        self.assertIn("openai_responses_vision", payload["preflight"])
        self.assertTrue(
            all(scenario["status"] == "blocked" for scenario in payload["scenarios"])
        )

    def test_supplied_explicit_blocked_run_stays_blocked_not_failed(self) -> None:
        run_dir = self.temp_dir() / "blocked-provider-run"
        run_dir.mkdir()
        self.write_json(
            run_dir / "run_status.json",
            {
                "status": "blocked_missing_visual_provider",
                "ok": False,
                "terminal": True,
                "selected_mode": "automated-cli",
            },
        )
        self.write_json(
            run_dir / "visual_provider_status.json",
            {
                "schema_version": "codex-deepresearch.visual-provider-status.v0",
                "run_id": run_dir.name,
                "status": "blocked_missing_visual_provider",
                "ok": False,
                "terminal": True,
                "metric_classification": "excluded_blocked",
                "providers": [
                    {
                        "provider": "brave-image-search",
                        "provider_kind": "web_image_search",
                        "provider_mode": "real",
                        "configured": False,
                        "available": False,
                        "blocked_reason": "missing_brave_search_api_key",
                        "invocations": 0,
                        "candidates_discovered": 0,
                        "artifacts_fetched": 0,
                        "vlm_images_analyzed": 0,
                        "estimated_cost_usd": 0.0,
                        "actual_cost_usd": 0.0,
                        "last_error": "missing_brave_search_api_key",
                    }
                ],
            },
        )

        result = evaluate_automated_visual_run(
            run_dir,
            scenario=self.scenario("product_image_discovery"),
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["blockers"][0]["classification"], "provider")

    def test_failures_are_classified_across_provider_fetch_policy_vlm_contradiction_and_report(
        self,
    ) -> None:
        runs_dir = self.temp_dir()
        run_dir = self.write_real_scenario_run(
            runs_dir / "product",
            scenario_id="product_image_discovery",
            candidate_count=9,
            observation_count=2,
        )
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        candidates[0]["policy_decision"] = "blocked"
        self.write_jsonl(run_dir / "visual_candidates.jsonl", candidates)
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", [])
        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        for provider in provider_status["providers"]:
            provider["candidates_discovered"] = 0
            provider["artifacts_fetched"] = 0
        provider_status["providers"][-1]["vlm_images_analyzed"] = 0
        self.write_json(run_dir / "visual_provider_status.json", provider_status)
        self.write_json(run_dir / "report_status.json", {"used_images": []})
        (run_dir / "report.md").write_text("No cited image-backed claim.\n", encoding="utf-8")

        with self.assertRaises(AutomatedVisualE2EError) as raised:
            run_automated_visual_e2e(
                runs_dir=self.temp_dir(),
                suite_id="failed-gate",
                clean=True,
                scenario_runs={"product_image_discovery": run_dir},
            )

        payload = self.read_json(raised.exception.results_path)
        self.assertEqual(payload["status"], "failed")
        for classification in (
            "provider",
            "fetch",
            "policy",
            "vlm",
            "contradiction",
            "report-linkage",
        ):
            self.assertGreater(
                payload["classification_counts"][classification],
                0,
                classification,
            )

    def test_evaluate_one_run_excludes_fixture_manual_and_user_provided_counts(self) -> None:
        run_dir = self.write_real_scenario_run(
            self.temp_dir() / "mixed-provider-run",
            scenario_id="product_image_discovery",
            include_non_release_records=True,
        )

        result = evaluate_automated_visual_run(
            run_dir,
            scenario=self.scenario("product_image_discovery"),
        )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["counts"]["scenario_real_candidates"], 10)
        self.assertEqual(result["counts"]["real_openai_responses_vision_observations"], 3)
        self.assertGreater(result["counts"]["excluded_non_release_records"], 0)

    def test_strict_gate_rejects_unavailable_openai_provider_status_with_real_counters(
        self,
    ) -> None:
        run_dir = self.write_real_scenario_run(
            self.temp_dir() / "unavailable-openai-provider-run",
            scenario_id="product_image_discovery",
        )
        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        openai_provider = provider_status["providers"][-1]
        self.assertEqual(openai_provider["provider"], "openai-responses-vision")
        openai_provider["configured"] = False
        openai_provider["available"] = False
        openai_provider["blocked_reason"] = "missing_openai_api_key"
        openai_provider["last_error"] = "missing_openai_api_key"
        self.write_json(run_dir / "visual_provider_status.json", provider_status)

        result = evaluate_automated_visual_run(
            run_dir,
            scenario=self.scenario("product_image_discovery"),
        )

        failure_checks = {failure["check"] for failure in result["failures"]}
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["counts"]["real_openai_responses_vision_observations"], 3)
        self.assertIn("openai_responses_vision_provider_status", failure_checks)
        self.assertNotIn("openai_responses_vision_image_floor", failure_checks)

    def test_report_cited_claim_requires_real_openai_observation_for_that_image(
        self,
    ) -> None:
        run_dir = self.write_real_scenario_run(
            self.temp_dir() / "fixture-cited-observation-run",
            scenario_id="product_image_discovery",
            observation_count=4,
        )
        observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
        for observation in observations:
            if observation["evidence_image_id"] != "img_real_001":
                continue
            observation["provider_mode"] = "fixture"
            observation["provider_provenance"] = {
                "provider": "openai-responses-vision",
                "provider_kind": "vlm",
                "provider_mode": "fixture",
                "external_vlm_call": False,
            }
            observation["estimated_cost_usd"] = 0.0
            observation["actual_cost_usd"] = 0.0
        self.write_jsonl(run_dir / "visual_observations.jsonl", observations)

        result = evaluate_automated_visual_run(
            run_dir,
            scenario=self.scenario("product_image_discovery"),
        )

        failure_checks = {failure["check"] for failure in result["failures"]}
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["counts"]["real_openai_responses_vision_observations"], 3)
        self.assertIn("report_cited_visual_or_mixed_claim", failure_checks)
        self.assertNotIn("openai_responses_vision_image_floor", failure_checks)
        self.assertNotIn("openai_responses_vision_provider_status", failure_checks)

    def test_cli_outputs_blocked_diagnostics_with_allow_blocked(self) -> None:
        command = subprocess.run(
            [
                str(RUNNER),
                "automated-visual-e2e",
                "--runs-dir",
                str(self.temp_dir()),
                "--suite-id",
                "cli-blocked",
                "--clean",
                "--allow-blocked",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(command.returncode, 0, command.stderr)
        payload = json.loads(command.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["outcome_counts"]["blocked"], 4)

    def scenario(self, scenario_id: str):
        from deepresearch.automated_visual_e2e import DEFAULT_AUTOMATED_VISUAL_SCENARIOS

        return next(scenario for scenario in DEFAULT_AUTOMATED_VISUAL_SCENARIOS if scenario.id == scenario_id)

    def write_real_scenario_run(
        self,
        run_dir: Path,
        *,
        scenario_id: str,
        candidate_count: int = 10,
        observation_count: int = 3,
        include_non_release_records: bool = False,
    ) -> Path:
        provider_kind, origin, target_evidence_type = SCENARIOS[scenario_id]
        run_dir.mkdir(parents=True)
        run_id = run_dir.name
        created_at = "2026-06-25T00:00:00Z"
        task_id = "task_research_001"
        angle_id = "angle_001"
        provider = f"real-{provider_kind.replace('_', '-')}"
        candidates: list[dict[str, Any]] = []
        fetches: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        images: list[dict[str, Any]] = []
        for index in range(1, candidate_count + 1):
            candidate = self.candidate_record(
                index=index,
                task_id=task_id,
                angle_id=angle_id,
                provider=provider,
                provider_kind=provider_kind,
                origin=origin,
                status="analyzed" if index <= observation_count else "ranked",
            )
            candidates.append(candidate)
            if index <= observation_count:
                image_id = f"img_real_{index:03d}"
                fetch = self.fetch_record(candidate, index=index, evidence_image_id=image_id)
                fetches.append(fetch)
                images.append(self.evidence_image(candidate, fetch, image_id))
                observations.append(
                    self.observation_record(
                        candidate,
                        fetch,
                        evidence_image_id=image_id,
                        claim_id="claim_visual_001" if index == 1 else None,
                        vote_id="vote_visual_001" if index == 1 else None,
                    )
                )
        if include_non_release_records:
            for mode in ("fixture", "manual", "user_provided"):
                candidates.append(
                    {
                        **self.candidate_record(
                            index=len(candidates) + 1,
                            task_id=task_id,
                            angle_id=angle_id,
                            provider=f"{mode}-provider",
                            provider_kind="fixture" if mode == "fixture" else "manual",
                            origin="image_search",
                            status="ranked",
                        ),
                        "provider_mode": mode,
                        "provider_provenance": {
                            "provider": f"{mode}-provider",
                            "provider_kind": "fixture" if mode == "fixture" else "manual",
                            "provider_mode": mode,
                        },
                    }
                )
        self.write_json(
            run_dir / "research_tasks.json",
            {
                "schema_version": "codex-deepresearch.parallel.v0",
                "run_id": run_id,
                "created_at": created_at,
                "tasks": [
                    {
                        "id": task_id,
                        "angle_id": angle_id,
                        "route": "visual_required",
                        "query": scenario_id,
                    }
                ],
            },
        )
        self.write_json(
            run_dir / "visual_tasks.json",
            {
                "schema_version": "codex-deepresearch.search-handoff.v0",
                "run_id": run_id,
                "created_at": created_at,
                "tasks": [
                    {
                        "id": "task_visual_001",
                        "angle_id": angle_id,
                        "route": "visual_required",
                    }
                ],
            },
        )
        self.write_json(
            run_dir / "visual_search_plan.json",
            {
                "schema_version": "codex-deepresearch.visual-artifacts.v0",
                "run_id": run_id,
                "created_at": created_at,
                "tasks": [
                    {
                        "plan_id": "plan_visual_001",
                        "task_id": task_id,
                        "angle_id": angle_id,
                        "route": "visual_required",
                        "target_evidence_type": target_evidence_type,
                        "query": scenario_id,
                        "providers": [provider],
                        "source_search_result_ids": [],
                        "caps": {
                            "max_candidates": candidate_count,
                            "max_fetches": observation_count,
                            "max_vlm_images": observation_count,
                            "max_cost_usd": 1.0,
                        },
                        "policy_constraints": {"robots": "allowed"},
                        "estimated_cost_usd": 0.1,
                        "state": "completed",
                    }
                ],
            },
        )
        self.write_jsonl(run_dir / "visual_candidates.jsonl", candidates)
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", fetches)
        self.write_jsonl(run_dir / "visual_observations.jsonl", observations)
        self.write_jsonl(run_dir / "verifier_votes.jsonl", [{"id": "vote_visual_001"}])
        self.write_json(
            run_dir / "visual_provider_status.json",
            {
                "schema_version": "codex-deepresearch.visual-provider-status.v0",
                "run_id": run_id,
                "status": "completed_auto_visual",
                "ok": True,
                "terminal": True,
                "metric_classification": "success",
                "providers": [
                    self.provider_status(
                        provider=provider,
                        provider_kind=provider_kind,
                        candidates_discovered=candidate_count,
                        artifacts_fetched=observation_count,
                        vlm_images_analyzed=0,
                        invocations=1,
                    ),
                    self.provider_status(
                        provider="openai-responses-vision",
                        provider_kind="vlm",
                        candidates_discovered=0,
                        artifacts_fetched=observation_count,
                        vlm_images_analyzed=observation_count,
                        invocations=observation_count,
                    ),
                ],
            },
        )
        self.write_json(
            run_dir / "evidence.json",
            {
                "schema_version": "0.1.0",
                "run_id": run_id,
                "created_at": created_at,
                "question": scenario_id,
                "mode": "automated-cli",
                "search_provider": "brave",
                "vlm_provider": "openai-responses-vision",
                "search_tasks": [
                    {
                        "id": task_id,
                        "angle_id": angle_id,
                        "route": "visual_required",
                        "query": scenario_id,
                    }
                ],
                "images": images,
                "claims": [
                    {
                        "id": "claim_visual_001",
                        "text": "The real visual provider image supports the claim.",
                        "claim_type": "visual",
                        "supporting_sources": [],
                        "supporting_images": ["img_real_001"],
                        "visual_supports": [
                            {
                                "image_id": "img_real_001",
                                "observation_ref": "images.img_real_001.observations[0]",
                            }
                        ],
                        "verification_status": "supported",
                        "review_status": "auto_reviewed",
                        "promotion_status": "not_eligible",
                        "confidence": "high",
                        "votes": [{"id": "vote_visual_001"}],
                    }
                ],
            },
        )
        self.write_json(
            run_dir / "report_status.json",
            {
                "schema_version": "codex-deepresearch.report-generation.v0",
                "run_id": run_id,
                "status": "completed",
                "used_images": ["img_real_001"],
            },
        )
        (run_dir / "report.md").write_text(
            "## Findings\n\n"
            "- Claim `claim_visual_001` is supported by Image `img_real_001`.\n",
            encoding="utf-8",
        )
        self.write_json(
            run_dir / "run_status.json",
            {
                "schema_version": "codex-deepresearch.run-status.v0",
                "run_id": run_id,
                "run_dir": str(run_dir),
                "status": "completed_auto_visual",
                "ok": True,
                "terminal": True,
                "selected_mode": "automated-cli",
                "artifacts": {
                    "run_status": str(run_dir / "run_status.json"),
                    "visual_provider_status": str(run_dir / "visual_provider_status.json"),
                    "evidence": str(run_dir / "evidence.json"),
                    "report": str(run_dir / "report.md"),
                    "report_status": str(run_dir / "report_status.json"),
                },
            },
        )
        return run_dir

    def candidate_record(
        self,
        *,
        index: int,
        task_id: str,
        angle_id: str,
        provider: str,
        provider_kind: str,
        origin: str,
        status: str,
    ) -> dict[str, Any]:
        return {
            "candidate_id": f"cand_real_{index:03d}",
            "plan_id": "plan_visual_001",
            "task_id": task_id,
            "angle_id": angle_id,
            "provider": provider,
            "provider_kind": provider_kind,
            "provider_mode": "real",
            "provider_run_id": "run_real_001",
            "provider_provenance": {
                "provider": provider,
                "provider_kind": provider_kind,
                "provider_mode": "real",
                "external_network_call": provider_kind in {"web_image_search", "screenshot"},
            },
            "origin": origin,
            "page_url": f"https://example.com/page/{index}",
            "image_url": f"https://images.example.com/image-{index}.png",
            "rank": index,
            "score": max(0.1, 1.0 - (index * 0.01)),
            "policy_decision": "allowed",
            "policy_flags": [],
            "candidate_status": status,
            "rejection_reason": None,
            "estimated_cost_usd": 0.01,
            "actual_cost_usd": 0.01,
        }

    def fetch_record(
        self,
        candidate: dict[str, Any],
        *,
        index: int,
        evidence_image_id: str,
    ) -> dict[str, Any]:
        return {
            "fetch_id": f"fetch_real_{index:03d}",
            "candidate_id": candidate["candidate_id"],
            "task_id": candidate["task_id"],
            "angle_id": candidate["angle_id"],
            "provider": candidate["provider"],
            "provider_kind": candidate["provider_kind"],
            "provider_mode": candidate["provider_mode"],
            "provider_run_id": candidate["provider_run_id"],
            "provider_provenance": deepcopy(candidate["provider_provenance"]),
            "origin": candidate["origin"],
            "fetch_status": "fetched",
            "http_status": 200,
            "mime_type": "image/png",
            "byte_size": 1024 + index,
            "width": 800,
            "height": 600,
            "hash": f"sha256:img{index:03d}",
            "phash": f"phash:img{index:03d}",
            "local_artifact_path": f"images/{evidence_image_id}.png",
            "evidence_image_id": evidence_image_id,
            "policy_decision": "allowed",
            "policy_flags": [],
            "failure_code": None,
            "estimated_cost_usd": 0.01,
            "actual_cost_usd": 0.01,
        }

    def observation_record(
        self,
        candidate: dict[str, Any],
        fetch: dict[str, Any],
        *,
        evidence_image_id: str,
        claim_id: str | None,
        vote_id: str | None,
    ) -> dict[str, Any]:
        verifier_links = []
        report_links = []
        if claim_id:
            verifier_links.append(
                {
                    "claim_id": claim_id,
                    "visual_support_ref": f"images.{evidence_image_id}.observations[0]",
                    "verifier_vote_id": vote_id,
                }
            )
            report_links.append(
                {
                    "claim_id": claim_id,
                    "report_section_id": "findings",
                    "citation_id": f"img:{evidence_image_id}",
                }
            )
        return {
            "observation_id": f"obs_{evidence_image_id}",
            "evidence_image_id": evidence_image_id,
            "task_id": candidate["task_id"],
            "angle_id": candidate["angle_id"],
            "candidate_id": candidate["candidate_id"],
            "fetch_id": fetch["fetch_id"],
            "provider": "openai-responses-vision",
            "provider_kind": "vlm",
            "provider_mode": "real",
            "provider_run_id": "openai-real-001",
            "provider_provenance": {
                "provider": "openai-responses-vision",
                "provider_kind": "vlm",
                "provider_mode": "real",
                "external_vlm_call": True,
            },
            "model_or_tool": "gpt-4.1-mini",
            "observation_status": "analyzed",
            "observations": ["The image contains visible evidence."],
            "inferences": ["The image supports the visual claim."],
            "confidence": 0.9,
            "policy_decision": "allowed",
            "policy_flags": [],
            "caveats": [],
            "verifier_links": verifier_links,
            "report_links": report_links,
            "estimated_cost_usd": 0.02,
            "actual_cost_usd": 0.02,
            "created_at": "2026-06-25T00:00:00Z",
        }

    def evidence_image(
        self,
        candidate: dict[str, Any],
        fetch: dict[str, Any],
        evidence_image_id: str,
    ) -> dict[str, Any]:
        return {
            "id": evidence_image_id,
            "task_id": candidate["task_id"],
            "angle_id": candidate["angle_id"],
            "candidate_id": candidate["candidate_id"],
            "fetch_id": fetch["fetch_id"],
            "local_artifact_path": fetch["local_artifact_path"],
            "hash": fetch["hash"],
            "provider": candidate["provider"],
            "provider_kind": candidate["provider_kind"],
            "provider_mode": candidate["provider_mode"],
            "provider_provenance": deepcopy(candidate["provider_provenance"]),
            "policy_decision": "allowed",
            "estimated_cost_usd": 0.03,
            "actual_cost_usd": 0.03,
            "observations": ["The image contains visible evidence."],
        }

    def provider_status(
        self,
        *,
        provider: str,
        provider_kind: str,
        candidates_discovered: int,
        artifacts_fetched: int,
        vlm_images_analyzed: int,
        invocations: int,
    ) -> dict[str, Any]:
        return {
            "provider": provider,
            "provider_kind": provider_kind,
            "provider_mode": "real",
            "configured": True,
            "available": True,
            "blocked_reason": None,
            "invocations": invocations,
            "candidates_discovered": candidates_discovered,
            "artifacts_fetched": artifacts_fetched,
            "vlm_images_analyzed": vlm_images_analyzed,
            "estimated_cost_usd": 0.01,
            "actual_cost_usd": 0.01,
            "last_error": None,
        }


if __name__ == "__main__":
    unittest.main()
