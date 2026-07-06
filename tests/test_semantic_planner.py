from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch.parallel_orchestrator import plan_research_tasks, run_parallel_orchestration  # noqa: E402
from deepresearch.report_generation import synthesize_report  # noqa: E402
from deepresearch.search_handoff import prepare_run  # noqa: E402
from deepresearch.semantic_planner import (  # noqa: E402
    ALLOWED_EVIDENCE_NEEDS,
    PLANNER_MODE_BLOCKED,
    PLANNER_MODE_FIXTURE,
    PLANNER_MODE_HEURISTIC_TEMPLATE_FALLBACK,
    PLANNER_MODE_MANUAL_ANGLES,
    SEMANTIC_FIT_SCORE_THRESHOLD,
    heuristic_template_planner,
    semantic_planner_validation,
)


SEMANTIC_FIXTURES = [
    {
        "fixture_id": "technical_api",
        "question_class": "technical_api",
        "question": "Next.js 최신 App Router 캐싱 변경이 우리 SaaS에 미치는 영향 조사",
        "expected_needs": {
            "official_source",
            "recent_change",
            "implementation_detail",
            "failure_pattern",
            "risk_or_guardrail",
        },
        "expects_visual_route": False,
        "critical_query_tokens": ["next.js", "app router", "캐싱", "saas"],
    },
    {
        "fixture_id": "product_market",
        "question_class": "product_market",
        "question": "AI 회의록 서비스 시장과 경쟁 제품을 조사해줘",
        "expected_needs": {
            "primary_source",
            "comparative_analysis",
            "pricing_or_limits",
            "user_workflow",
            "risk_or_guardrail",
        },
        "expects_visual_route": False,
        "critical_query_tokens": ["ai", "회의록", "서비스"],
    },
    {
        "fixture_id": "visual_style",
        "question_class": "visual_style",
        "question": "일반 사진을 스냅사진처럼 보이게 만드는 방법 조사",
        "expected_needs": {
            "visual_example",
            "visual_observation",
            "comparative_analysis",
            "implementation_detail",
            "failure_pattern",
            "policy_or_legal",
        },
        "expects_visual_route": True,
        "critical_query_tokens": ["사진", "스냅사진"],
    },
    {
        "fixture_id": "policy_risk",
        "question_class": "policy_risk",
        "question": "AI 생성 이미지 서비스를 출시할 때 필요한 표시/워터마크/초상권 리스크 조사",
        "expected_needs": {
            "official_source",
            "policy_or_legal",
            "risk_or_guardrail",
            "user_workflow",
            "comparative_analysis",
        },
        "expects_visual_route": False,
        "critical_query_tokens": ["워터마크", "초상권", "리스크"],
    },
    {
        "fixture_id": "implementation_architecture",
        "question_class": "implementation_architecture",
        "question": (
            "Codex DeepResearch runner에 semantic planner fan-out을 안정적으로 추가하려면 "
            "어떤 아키텍처와 테스트 전략이 필요한지 조사"
        ),
        "expected_needs": {
            "primary_source",
            "implementation_detail",
            "comparative_analysis",
            "failure_pattern",
            "user_workflow",
            "risk_or_guardrail",
        },
        "expects_visual_route": False,
        "critical_query_tokens": ["codex", "deepresearch", "semantic planner", "fan-out"],
    },
]


class SemanticPlannerTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def load_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def read_jsonl(self, path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def prepare_fixture(self, fixture: dict) -> Path:
        fallback_plan = heuristic_template_planner(question=fixture["question"])
        result = prepare_run(
            question=fixture["question"],
            runs_dir=self.temp_runs_dir(),
            angles=[angle.title for angle in fallback_plan.angles],
        )
        return Path(result["run_dir"])

    def assert_release_ineligible_semantic_validation(
        self,
        validation: dict,
        *,
        planner_mode: str,
    ) -> None:
        self.assertFalse(validation["ok"])
        self.assertEqual(validation["planner_mode"], planner_mode)
        self.assertFalse(validation["semantic_release_eligible"])
        codes = {failure["code"] for failure in validation["failures"]}
        self.assertIn("semantic_release_ineligible", codes)
        self.assertIn("release_ineligible_planner_mode", codes)

    def assert_integrity_artifacts(
        self,
        run_dir: Path,
        *,
        planner_mode: str,
    ) -> None:
        for filename in (
            "semantic_expectation_oracle.json",
            "semantic_plan.json",
            "semantic_plan_review.json",
            "semantic_plan_delta.json",
            "semantic_materialization_diff.json",
            "semantic_planner_raw/planner_request.json",
            "semantic_planner_raw/planner_response.json",
        ):
            self.assertTrue((run_dir / filename).is_file(), filename)
        review = self.load_json(run_dir / "semantic_plan_review.json")
        self.assertEqual(review["planner_mode"], planner_mode)
        self.assertFalse(review["semantic_release_eligible"])
        self.assertNotEqual(review.get("semantic_fit_score"), SEMANTIC_FIT_SCORE_THRESHOLD)
        self.assertEqual(review["final_verdict"], "release_ineligible")
        self.assertFalse(review["substitute_implementation_check"]["passed"])

    def test_required_broad_question_classes_create_semantic_angles_and_tasks(self) -> None:
        for fixture in SEMANTIC_FIXTURES:
            with self.subTest(fixture=fixture["fixture_id"]):
                fallback_plan = heuristic_template_planner(question=fixture["question"])
                self.assertTrue(
                    fixture["expected_needs"].issubset(
                        set(fallback_plan.expected_evidence_needs)
                    )
                )
                run_dir = self.prepare_fixture(fixture)
                evidence = self.load_json(run_dir / "evidence.json")
                validation = self.load_json(run_dir / "semantic_planner_validation.json")

                self.assert_release_ineligible_semantic_validation(
                    validation,
                    planner_mode=PLANNER_MODE_MANUAL_ANGLES,
                )
                self.assert_integrity_artifacts(
                    run_dir,
                    planner_mode=PLANNER_MODE_MANUAL_ANGLES,
                )
                real_smoke = validation["real_codex_exec_smoke"]
                self.assertIn(real_smoke["status"], {"completed", "skipped"})
                if real_smoke["status"] == "skipped":
                    self.assertIn("skip_category", real_smoke)
                    self.assertIn("reason", real_smoke)
                self.assertEqual(evidence["semantic_planner"]["question_class"], fixture["question_class"])
                self.assertEqual(
                    evidence["semantic_planner"]["source"],
                    "manual_angles",
                )
                self.assertEqual(
                    evidence["semantic_planner"]["planner_mode"],
                    PLANNER_MODE_MANUAL_ANGLES,
                )
                self.assertFalse(evidence["semantic_planner"]["semantic_release_eligible"])
                self.assertIsInstance(evidence["semantic_planner"]["broad_question"], bool)
                self.assertEqual(validation["question_class"], fixture["question_class"])
                self.assertGreaterEqual(validation["angle_count"], 5)
                self.assertLessEqual(validation["angle_count"], 8)

                angles = evidence["semantic_angles"]
                for angle in angles:
                    self.assertEqual(angle.get("question_context"), "")
                    self.assertIn(angle["evidence_need"], ALLOWED_EVIDENCE_NEEDS)
                    for field in (
                        "angle_id",
                        "title",
                        "research_question",
                        "question_context",
                        "route",
                        "evidence_need",
                        "expected_artifacts",
                        "success_criteria",
                        "report_section",
                    ):
                        self.assertIn(field, angle)
                        if field != "question_context":
                            self.assertTrue(angle[field], field)

                self.assertGreaterEqual(len(validation["covered_evidence_needs"]), 1)
                route_counts = validation["route_counts"]
                visual_route_count = int(route_counts.get("visual_required", 0)) + int(
                    route_counts.get("visual_optional", 0)
                )
                if fixture["expects_visual_route"]:
                    self.assertGreaterEqual(visual_route_count, 1)

                planned = plan_research_tasks(run=run_dir, min_tasks=1)
                tasks = planned["tasks"]
                self.assertEqual(len(tasks), validation["angle_count"])
                self.assertTrue(all(task["angle_id"] for task in tasks))
                for task in tasks:
                    lowered_query = task["query"].lower()
                    for token in fixture["critical_query_tokens"]:
                        self.assertIn(token.lower(), lowered_query)
                    for field in (
                        "query",
                        "route",
                        "expected_evidence",
                        "max_sources",
                        "max_images",
                        "success_criteria",
                        "output_shard_path",
                    ):
                        self.assertIn(field, task)
                        if field not in {"max_images"}:
                            self.assertTrue(task[field], field)

                planned_validation = self.load_json(run_dir / "semantic_planner_validation.json")
                self.assert_release_ineligible_semantic_validation(
                    planned_validation,
                    planner_mode=PLANNER_MODE_MANUAL_ANGLES,
                )

    def test_standard_preset_expands_semantic_angles_to_at_least_twenty_tasks(self) -> None:
        run_dir = self.prepare_fixture(SEMANTIC_FIXTURES[0])

        planned = plan_research_tasks(run=run_dir, min_tasks=20)
        tasks = planned["tasks"]

        self.assertGreaterEqual(len(tasks), 20)
        self.assertEqual(len(tasks), len(self.load_json(run_dir / "research_tasks.json")["tasks"]))
        self.assertGreaterEqual(len({task["angle_id"] for task in tasks}), 5)
        validation = self.load_json(run_dir / "semantic_planner_validation.json")
        self.assert_release_ineligible_semantic_validation(
            validation,
            planner_mode=PLANNER_MODE_MANUAL_ANGLES,
        )
        self.assertLessEqual(validation["near_duplicate_ratio"], 0.20)
        self.assertLessEqual(validation["generic_lens_ratio"], 0.30)

    def test_fixture_e2e_proves_distinct_subagent_scopes_and_report_angle_coverage(self) -> None:
        run_dir = self.prepare_fixture(SEMANTIC_FIXTURES[2])

        parallel = run_parallel_orchestration(run=run_dir, adapter_name="fixture", min_tasks=1)
        report = synthesize_report(run=run_dir)

        self.assertEqual(parallel["status"], "completed_fixture")
        self.assertGreaterEqual(len(parallel["merge"]["accepted_shards"]), 5)
        self.assertEqual(report["status"], "completed")

        tasks = self.load_json(run_dir / "research_tasks.json")["tasks"]
        assignments = self.read_jsonl(run_dir / "subagent_assignments.jsonl")
        scope_hashes = {record["task_scope_hash"] for record in assignments}
        shard_paths = {task["output_shard_path"] for task in tasks}
        self.assertEqual(len(scope_hashes), len(tasks))
        self.assertEqual(len(shard_paths), len(tasks))
        self.assertTrue(all((run_dir / path).is_file() for path in shard_paths))

        merge_status = self.load_json(run_dir / "merge_status.json")
        report_status = self.load_json(run_dir / "report_status.json")
        validation = self.load_json(run_dir / "semantic_planner_validation.json")
        self.assertEqual(merge_status["status"], "completed")
        self.assertEqual(report_status["status"], "completed")
        self.assert_release_ineligible_semantic_validation(
            validation,
            planner_mode=PLANNER_MODE_MANUAL_ANGLES,
        )
        self.assertGreaterEqual(
            len([count for count in validation["report_angle_claim_counts"].values() if count > 0]),
            3,
        )
        self.assertGreaterEqual(validation["visual_expected_evidence_hits"]["visual_example"], 1)
        visual_observation_hits = validation["visual_expected_evidence_hits"].get(
            "visual_observation",
            0,
        ) + validation["visual_expected_evidence_hits"].get("vlm_analysis", 0)
        self.assertGreaterEqual(visual_observation_hits, 1)

    def test_primary_source_only_broad_question_fails_validation(self) -> None:
        run_dir = self.temp_runs_dir()
        evidence = {
            "run_id": "semantic-primary-only",
            "question": SEMANTIC_FIXTURES[0]["question"],
            "semantic_planner": {
                "fixture_id": "primary-only-negative",
                "question_class": "technical_api",
                "broad_question": True,
                "expected_evidence_needs": [
                    "official_source",
                    "recent_change",
                    "implementation_detail",
                    "failure_pattern",
                ],
            },
            "semantic_angles": [
                {
                    "angle_id": "angle_001",
                    "title": "Primary source discovery",
                    "research_question": SEMANTIC_FIXTURES[0]["question"],
                    "route": "text_only",
                    "evidence_need": "primary_source",
                    "expected_artifacts": ["source list"],
                    "success_criteria": ["Find one source."],
                    "report_section": "Primary Sources",
                }
            ],
        }

        validation = semantic_planner_validation(run_dir=run_dir, evidence=evidence, tasks=[])

        self.assertFalse(validation["ok"])
        codes = {failure["code"] for failure in validation["failures"]}
        self.assertIn("primary_source_discovery_only", codes)
        self.assertIn("broad_question_angle_count_out_of_range", codes)

    def test_suffix_duplicate_and_generic_fanout_fails_validation(self) -> None:
        run_dir = self.prepare_fixture(SEMANTIC_FIXTURES[0])
        evidence = self.load_json(run_dir / "evidence.json")
        root = evidence["question"]
        tasks = [
            {
                "id": f"task_bad_{index:03d}",
                "angle_id": "angle_001",
                "route": "text_only",
                "query": f"{root} :: {lens} #{index:03d}",
                "expected_evidence": ["official_source"],
                "max_images": 0,
            }
            for index, lens in enumerate(
                [
                    "official documentation",
                    "primary sources",
                    "recent changes",
                    "official documentation",
                    "primary sources",
                ],
                start=1,
            )
        ]

        validation = semantic_planner_validation(run_dir=run_dir, evidence=evidence, tasks=tasks)

        self.assertFalse(validation["ok"])
        self.assertGreater(validation["near_duplicate_ratio"], 0.20)
        self.assertGreater(validation["generic_lens_ratio"], 0.30)
        codes = {failure["code"] for failure in validation["failures"]}
        self.assertIn("near_duplicate_task_ratio_exceeded", codes)
        self.assertIn("generic_lens_task_ratio_exceeded", codes)

    def test_visual_question_with_all_text_routes_fails_validation(self) -> None:
        run_dir = self.prepare_fixture(SEMANTIC_FIXTURES[2])
        evidence = self.load_json(run_dir / "evidence.json")
        for angle in evidence["semantic_angles"]:
            angle["route"] = "text_only"
            angle["expected_evidence"] = [angle["evidence_need"]]
        for route in evidence["routing"]:
            route["modality"] = "text_only"

        validation = semantic_planner_validation(run_dir=run_dir, evidence=evidence, tasks=[])

        self.assertFalse(validation["ok"])
        codes = {failure["code"] for failure in validation["failures"]}
        self.assertIn("visual_question_all_text_only", codes)

    def test_text_only_technical_question_with_irrelevant_visual_task_fails_validation(self) -> None:
        run_dir = self.prepare_fixture(SEMANTIC_FIXTURES[0])
        evidence = self.load_json(run_dir / "evidence.json")
        tasks = [
            {
                "id": "task_bad_visual_001",
                "angle_id": "angle_001",
                "route": "visual_required",
                "query": "Irrelevant image collection for a text API question.",
                "expected_evidence": ["visual_example"],
                "max_images": 4,
            }
        ]

        validation = semantic_planner_validation(run_dir=run_dir, evidence=evidence, tasks=tasks)

        self.assertFalse(validation["ok"])
        codes = {failure["code"] for failure in validation["failures"]}
        self.assertIn("irrelevant_visual_tasks_for_text_only_question", codes)
        self.assertIn("irrelevant_visual_expected_evidence_for_text_question", codes)

    def test_explicit_angle_input_continues_to_work(self) -> None:
        result = prepare_run(
            question="Research the product market and compare checkout UI.",
            runs_dir=self.temp_runs_dir(),
            angles=[
                "official API docs and release notes",
                "checkout UI screenshot comparison",
                "market report and competitor benchmark posts",
            ],
        )
        run_dir = Path(result["run_dir"])
        evidence = self.load_json(run_dir / "evidence.json")

        self.assertEqual(evidence["semantic_planner"]["source"], "manual_angles")
        self.assertEqual(evidence["semantic_planner"]["planner_mode"], PLANNER_MODE_MANUAL_ANGLES)
        self.assertFalse(evidence["semantic_planner"]["semantic_release_eligible"])
        validation = self.load_json(run_dir / "semantic_planner_validation.json")
        self.assert_release_ineligible_semantic_validation(
            validation,
            planner_mode=PLANNER_MODE_MANUAL_ANGLES,
        )
        self.assertEqual(
            [angle["title"] for angle in evidence["semantic_angles"]],
            [
                "official API docs and release notes",
                "checkout UI screenshot comparison",
                "market report and competitor benchmark posts",
            ],
        )
        self.assertEqual(
            [task["route"] for task in evidence["search_tasks"]],
            ["text_only", "visual_required", "visual_optional"],
        )

    def test_route_override_preserves_heuristic_fanout_and_overrides_routes(self) -> None:
        fallback_plan = heuristic_template_planner(question=SEMANTIC_FIXTURES[0]["question"])
        result = prepare_run(
            question=SEMANTIC_FIXTURES[0]["question"],
            runs_dir=self.temp_runs_dir(),
            angles=[angle.title for angle in fallback_plan.angles],
            route="text_only",
        )
        run_dir = Path(result["run_dir"])
        evidence = self.load_json(run_dir / "evidence.json")

        self.assertEqual(evidence["semantic_planner"]["source"], "manual_angles")
        self.assertEqual(
            evidence["semantic_planner"]["planner_mode"],
            PLANNER_MODE_MANUAL_ANGLES,
        )
        self.assertFalse(evidence["semantic_planner"]["semantic_release_eligible"])
        self.assertEqual(evidence["semantic_planner"]["question_class"], "technical_api")
        self.assertGreaterEqual(len(evidence["semantic_angles"]), 5)
        self.assertLessEqual(len(evidence["semantic_angles"]), 8)
        self.assertTrue(all(angle["route"] == "text_only" for angle in evidence["semantic_angles"]))

        planned = plan_research_tasks(run=run_dir, min_tasks=1)
        self.assertEqual(len(planned["tasks"]), len(evidence["semantic_angles"]))
        self.assertTrue(all(task["route"] == "text_only" for task in planned["tasks"]))
        for task in planned["tasks"]:
            lowered_query = task["query"].lower()
            for token in SEMANTIC_FIXTURES[0]["critical_query_tokens"]:
                self.assertIn(token.lower(), lowered_query)

    def test_visual_route_override_without_angles_blocks_before_task_materialization(self) -> None:
        for route in ("visual_required", "visual_optional"):
            with self.subTest(route=route):
                result = prepare_run(
                    question="Find image evidence for a public product interface",
                    runs_dir=self.temp_runs_dir(),
                    route=route,
                    budget_preset="quick",
                )
                run_dir = Path(result["run_dir"])
                evidence = self.load_json(run_dir / "evidence.json")

                self.assertEqual(result["status"], "blocked_semantic_planner_unavailable")
                self.assertEqual(
                    evidence["semantic_planner"]["source"],
                    "blocked_semantic_planner_unavailable",
                )
                self.assertEqual(
                    evidence["semantic_planner"]["planner_mode"],
                    PLANNER_MODE_BLOCKED,
                )
                self.assertFalse(evidence["semantic_planner"]["semantic_release_eligible"])
                self.assertEqual(evidence["semantic_planner"]["status"], "blocked_semantic_planner_unavailable")
                self.assertEqual(evidence["semantic_angles"], [])
                self.assertEqual(evidence["routing"], [])
                self.assertFalse((run_dir / "search_tasks.json").exists())
                self.assertFalse((run_dir / "visual_tasks.json").exists())
                self.assertFalse((run_dir / "research_tasks.json").exists())
                self.assertFalse((run_dir / "parallel_orchestration_status.json").exists())

                validation = self.load_json(run_dir / "semantic_planner_validation.json")
                self.assert_release_ineligible_semantic_validation(
                    validation,
                    planner_mode=PLANNER_MODE_BLOCKED,
                )

    def test_visual_evidence_terms_win_over_implementation_wording(self) -> None:
        plan = heuristic_template_planner(
            question="UI screenshot comparison implementation strategy 조사",
        )
        routes = [angle.route for angle in plan.angles]

        self.assertEqual(plan.question_class, "visual_style")
        self.assertIn("visual_required", routes)

    def test_fixture_planner_mode_cannot_pass_semantic_validation(self) -> None:
        run_dir = self.temp_runs_dir()
        question = "Fixture-only planner should never count as semantic."
        plan = heuristic_template_planner(
            question=question,
            planner_mode=PLANNER_MODE_FIXTURE,
        )
        evidence = {
            "run_id": "fixture-release-ineligible",
            "question": question,
            "semantic_planner": plan.to_dict(),
            "semantic_angles": [angle.to_dict() for angle in plan.angles],
        }

        validation = semantic_planner_validation(run_dir=run_dir, evidence=evidence, tasks=[])

        self.assert_release_ineligible_semantic_validation(
            validation,
            planner_mode=PLANNER_MODE_FIXTURE,
        )

    def test_historical_ev_prompt_default_runner_cannot_claim_semantic_success(self) -> None:
        result = prepare_run(
            question="전기차 배터리 화재 안전 테스트 이미지와 규제 근거를 조사해줘",
            runs_dir=self.temp_runs_dir(),
        )
        run_dir = Path(result["run_dir"])
        evidence = self.load_json(run_dir / "evidence.json")
        validation = self.load_json(run_dir / "semantic_planner_validation.json")
        review = self.load_json(run_dir / "semantic_plan_review.json")

        self.assertNotEqual(evidence["semantic_planner"]["planner_mode"], "codex_semantic")
        self.assertFalse(evidence["semantic_planner"]["semantic_release_eligible"])
        self.assert_release_ineligible_semantic_validation(
            validation,
            planner_mode=PLANNER_MODE_BLOCKED,
        )
        self.assertEqual(
            evidence["semantic_planner"]["status"],
            "blocked_semantic_planner_unavailable",
        )
        self.assertFalse(review["substitute_implementation_check"]["passed"])
        self.assertNotEqual(review.get("semantic_fit_score"), SEMANTIC_FIT_SCORE_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
