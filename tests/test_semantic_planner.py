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
        result = prepare_run(
            question=fixture["question"],
            runs_dir=self.temp_runs_dir(),
        )
        return Path(result["run_dir"])

    def test_required_broad_question_classes_create_semantic_angles_and_tasks(self) -> None:
        for fixture in SEMANTIC_FIXTURES:
            with self.subTest(fixture=fixture["fixture_id"]):
                run_dir = self.prepare_fixture(fixture)
                evidence = self.load_json(run_dir / "evidence.json")
                validation = self.load_json(run_dir / "semantic_planner_validation.json")

                self.assertTrue(validation["ok"], validation["failures"])
                real_smoke = validation["real_codex_exec_smoke"]
                self.assertIn(real_smoke["status"], {"completed", "skipped"})
                if real_smoke["status"] == "skipped":
                    self.assertIn("skip_category", real_smoke)
                    self.assertIn("reason", real_smoke)
                self.assertEqual(evidence["semantic_planner"]["question_class"], fixture["question_class"])
                self.assertTrue(evidence["semantic_planner"]["broad_question"])
                self.assertEqual(validation["question_class"], fixture["question_class"])
                self.assertGreaterEqual(validation["angle_count"], 5)
                self.assertLessEqual(validation["angle_count"], 8)

                angles = evidence["semantic_angles"]
                for angle in angles:
                    self.assertIn(angle["evidence_need"], ALLOWED_EVIDENCE_NEEDS)
                    for field in (
                        "angle_id",
                        "title",
                        "research_question",
                        "route",
                        "evidence_need",
                        "expected_artifacts",
                        "success_criteria",
                        "report_section",
                    ):
                        self.assertIn(field, angle)
                        self.assertTrue(angle[field], field)

                self.assertTrue(fixture["expected_needs"].issubset(validation["covered_evidence_needs"]))
                route_counts = validation["route_counts"]
                visual_route_count = int(route_counts.get("visual_required", 0)) + int(
                    route_counts.get("visual_optional", 0)
                )
                if fixture["expects_visual_route"]:
                    self.assertGreaterEqual(visual_route_count, 1)
                else:
                    self.assertEqual(visual_route_count, 0)

                planned = plan_research_tasks(run=run_dir, min_tasks=1)
                tasks = planned["tasks"]
                self.assertEqual(len(tasks), validation["angle_count"])
                self.assertTrue(all(task["angle_id"] for task in tasks))
                for task in tasks:
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
                self.assertTrue(planned_validation["ok"], planned_validation["failures"])
                self.assertLessEqual(planned_validation["near_duplicate_ratio"], 0.20)
                self.assertLessEqual(planned_validation["generic_lens_ratio"], 0.30)

    def test_standard_preset_expands_semantic_angles_to_at_least_twenty_tasks(self) -> None:
        run_dir = self.prepare_fixture(SEMANTIC_FIXTURES[0])

        planned = plan_research_tasks(run=run_dir, min_tasks=20)
        tasks = planned["tasks"]

        self.assertGreaterEqual(len(tasks), 20)
        self.assertEqual(len(tasks), len(self.load_json(run_dir / "research_tasks.json")["tasks"]))
        self.assertGreaterEqual(len({task["angle_id"] for task in tasks}), 5)
        validation = self.load_json(run_dir / "semantic_planner_validation.json")
        self.assertTrue(validation["ok"], validation["failures"])
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
        self.assertTrue(validation["ok"], validation["failures"])
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

        self.assertEqual(evidence["semantic_planner"]["source"], "explicit")
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


if __name__ == "__main__":
    unittest.main()
