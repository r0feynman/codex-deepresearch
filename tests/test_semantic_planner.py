from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch.parallel_orchestrator import (  # noqa: E402
    ParallelOrchestrationError,
    plan_research_tasks,
    run_parallel_orchestration,
)
from deepresearch.report_generation import synthesize_report  # noqa: E402
from deepresearch.search_handoff import prepare_run  # noqa: E402
from deepresearch.semantic_planner import (  # noqa: E402
    ALLOWED_EVIDENCE_NEEDS,
    CODEX_SEMANTIC_ADAPTER_INVOCATION_KIND,
    CODEX_SEMANTIC_PLANNER_COMMAND_ENV,
    PLANNER_MODE_BLOCKED,
    PLANNER_MODE_CODEX_SEMANTIC,
    PLANNER_MODE_FIXTURE,
    PLANNER_MODE_HEURISTIC_TEMPLATE_FALLBACK,
    PLANNER_MODE_MANUAL_ANGLES,
    SEMANTIC_FIT_SCORE_THRESHOLD,
    SemanticPlannerAdapterUnavailable,
    heuristic_template_planner,
    semantic_planner_validation,
    validate_semantic_candidate_plan,
    validate_codex_semantic_adapter_provenance,
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

    def write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def read_jsonl(self, path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def codex_adapter_response(
        self,
        request: dict,
        *,
        question_scope: str = "broad",
        angle_count: int = 5,
        tasks_per_angle: int = 4,
        requirement_types: tuple[str, ...] = ("subject",),
        visual_angle_indexes: tuple[int, ...] = (),
    ) -> dict:
        question = request["original_question"]
        constraints = []
        for index, requirement_type in enumerate(requirement_types, start=1):
            if requirement_type == "subject":
                prompt_text = question
                requirement_text = question
            elif requirement_type == "source_quality":
                prompt_text = "official primary sources"
                requirement_text = "Official, regulatory, or primary source evidence is required."
            elif requirement_type == "visual_modality":
                prompt_text = "images"
                requirement_text = "Visual evidence is required."
            elif requirement_type == "time_range":
                prompt_text = "2026"
                requirement_text = "Current or recent evidence is required."
            elif requirement_type == "geography":
                prompt_text = "Korea"
                requirement_text = "Korea scoped evidence is required."
            elif requirement_type == "deliverable_shape":
                prompt_text = "table"
                requirement_text = "A table or matrix deliverable is required."
            else:
                prompt_text = requirement_type
                requirement_text = requirement_type
            start = question.find(prompt_text)
            constraints.append(
                {
                    "requirement_id": f"req_{index:03d}",
                    "requirement_type": requirement_type,
                    "requirement_text": requirement_text,
                    "prompt_text": prompt_text,
                    "prompt_span": {
                        "start": start if start >= 0 else None,
                        "end": start + len(prompt_text) if start >= 0 else None,
                    },
                    "explicit": True,
                    "non_negotiable": True,
                    "inferred_reason": None,
                }
            )

        official = "source_quality" in requirement_types
        deliverable = "deliverable_shape" in requirement_types
        source_types = (
            ["official primary sources", "regulatory records"]
            if official
            else ["primary sources"]
        )
        angles = []
        bounded_tasks = []
        task_index = 1
        evidence_needs = (
            "primary_source",
            "official_source",
            "visual_observation",
            "recent_change",
            "comparative_analysis",
            "counter_evidence",
        )
        for angle_index in range(1, angle_count + 1):
            angle_id = f"angle_{angle_index:03d}"
            route = "visual_required" if angle_index in visual_angle_indexes else "text_only"
            visual_targets = [f"{question} image evidence"] if route != "text_only" else []
            evidence_need = evidence_needs[(angle_index - 1) % len(evidence_needs)]
            angles.append(
                {
                    "angle_id": angle_id,
                    "title": f"{question} adapter angle {angle_index}",
                    "research_question": f"What evidence answers {question} for angle {angle_index}?",
                    "why_this_angle_matters": f"Angle {angle_index} covers a distinct part of {question}.",
                    "included_scope": [question],
                    "excluded_scope": ["Do not substitute a local template inventory."],
                    "route": route,
                    "evidence_need": evidence_need,
                    "expected_source_types": source_types,
                    "expected_visual_targets": visual_targets,
                    "expected_artifacts": ["source matrix", "adapter evidence notes"],
                    "search_queries": [f"{question} adapter evidence {angle_index}"],
                    "success_criteria": [
                        f"Findings must directly address {question}.",
                        "Claims must cite source metadata.",
                    ],
                    "report_section": f"Adapter Angle {angle_index}",
                    "risk_or_contradiction_checks": ["Check contradictory evidence."],
                }
            )
            for _ in range(tasks_per_angle):
                expected_artifacts = ["source matrix", "adapter evidence notes"]
                success_criteria = [
                    f"Task must preserve the subject: {question}.",
                    "Task must produce source-backed findings.",
                ]
                if official:
                    success_criteria.append(
                        "Use official, regulatory, or primary sources for support."
                    )
                if deliverable:
                    expected_artifacts.append("requested table or matrix")
                    success_criteria.append("Preserve the requested table or matrix shape.")
                bounded_tasks.append(
                    {
                        "task_id": f"task_semantic_{task_index:03d}",
                        "angle_id": angle_id,
                        "query": (
                            f"{question} adapter bounded task {task_index} "
                            "official regulatory Korea 2026 table visual evidence"
                        ),
                        "route": route,
                        "freshness_requirement": (
                            "recent" if "time_range" in requirement_types else "any"
                        ),
                        "source_policy": {
                            "decision": "allowed",
                            "requires_official_or_primary": official,
                            "quality_requirements": (
                                ["official", "regulatory", "primary"]
                                if official
                                else ["source-backed"]
                            ),
                            "flags": [],
                        },
                        "expected_source_types": source_types,
                        "expected_visual_targets": visual_targets,
                        "expected_artifacts": expected_artifacts,
                        "success_criteria": success_criteria,
                        "max_sources": 3,
                        "max_images": 2 if route != "text_only" else 0,
                        "done_condition": (
                            "Stop when source-backed findings, caveats, and requested "
                            "deliverable details are recorded."
                        ),
                    }
                )
                task_index += 1

        task_ids_by_angle = {}
        for task in bounded_tasks:
            task_ids_by_angle.setdefault(task["angle_id"], []).append(task["task_id"])
        coverage = []
        for constraint in constraints:
            covered_angles = [angle["angle_id"] for angle in angles]
            coverage.append(
                {
                    **constraint,
                    "covered_by_angle_ids": covered_angles,
                    "covered_by_task_ids": [
                        task_id
                        for angle_id in covered_angles
                        for task_id in task_ids_by_angle.get(angle_id, [])
                    ],
                    "coverage_status": "covered",
                }
            )
        return {
            "schema_version": "codex-deepresearch.semantic-planner.v0",
            "artifact_type": "semantic_planner_raw_response",
            "planner_mode": PLANNER_MODE_CODEX_SEMANTIC,
            "planner_adapter": request["planner_adapter"],
            "prompt_version": request["prompt_version"],
            "semantic_release_eligible": False,
            "model_or_surface": "codex-semantic-test-double",
            "provenance": {
                "adapter_invocation_id": "adapter-test-invocation-001",
                "adapter_invocation_kind": CODEX_SEMANTIC_ADAPTER_INVOCATION_KIND,
                "raw_response_id": "adapter-test-response-001",
                "raw_request_hash": request["adapter_request_hash"],
                "model_or_surface": "codex-semantic-test-double",
                "child_session_id": "codex-child-test-001",
            },
            "candidate_plan": {
                "schema_version": "codex-deepresearch.semantic-planner.v0",
                "planner_mode": PLANNER_MODE_CODEX_SEMANTIC,
                "semantic_release_eligible": False,
                "source": "codex_semantic",
                "model_or_surface": "codex-semantic-test-double",
                "original_question": question,
                "language": "en",
                "depth_preset": request["depth_preset"],
                "intent_summary": f"Adapter-planned research for {question}.",
                "domain_entities": [
                    {"name": question, "type": "question_subject", "evidence": question}
                ],
                "constraints": constraints,
                "question_scope": question_scope,
                "decomposition_strategy": "Adapter response splits the prompt into bounded tasks.",
                "requirement_coverage_map": coverage,
                "negative_scope": ["Do not use local deterministic template output."],
                "angles": angles,
                "bounded_tasks": bounded_tasks,
            },
        }

    def prepare_with_codex_adapter(
        self,
        question: str,
        *,
        stdout_format: str = "json",
        response_mutator: object | None = None,
        **adapter_kwargs: object,
    ) -> tuple[dict, dict]:
        captured: dict[str, dict] = {}

        def fake_run(
            command: list[str],
            *,
            check: bool,
            capture_output: bool,
            input: str,
            text: bool,
            timeout: float,
        ) -> subprocess.CompletedProcess[str]:
            self.assertEqual(command[:3], ["codex", "exec", "--json"])
            self.assertFalse(check)
            self.assertTrue(capture_output)
            self.assertTrue(text)
            self.assertGreater(timeout, 0)
            request = json.loads(input)
            captured["request"] = dict(request)
            response = self.codex_adapter_response(request, **adapter_kwargs)
            if callable(response_mutator):
                response = response_mutator(response)
            if stdout_format == "jsonl":
                stdout = "\n".join(
                    [
                        json.dumps(
                            {
                                "id": "codex-event-001",
                                "type": "session.started",
                                "session_id": "codex-session-jsonl-001",
                            },
                            sort_keys=True,
                        ),
                        json.dumps(
                            {
                                "id": "codex-event-002",
                                "type": "message",
                                "session_id": "codex-session-jsonl-001",
                                "response": response,
                            },
                            sort_keys=True,
                        ),
                    ]
                )
            else:
                stdout = json.dumps(response, sort_keys=True)
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        with mock.patch(
            "deepresearch.semantic_planner.subprocess.run",
            side_effect=fake_run,
        ), mock.patch.dict(
            "os.environ",
            {CODEX_SEMANTIC_PLANNER_COMMAND_ENV: "codex exec --json"},
        ):
            result = prepare_run(question=question, runs_dir=self.temp_runs_dir())
        return result, captured["request"]

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

    def assert_codex_candidate_release_ineligible(self, validation: dict) -> None:
        self.assertFalse(validation["ok"])
        self.assertEqual(validation["planner_mode"], PLANNER_MODE_CODEX_SEMANTIC)
        self.assertFalse(validation["semantic_release_eligible"])
        codes = {failure["code"] for failure in validation["failures"]}
        self.assertIn("semantic_release_ineligible", codes)
        self.assertNotIn("release_ineligible_planner_mode", codes)

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

    def assert_validation_common_integrity_fields(self, validation: dict, source: dict) -> None:
        common_fields = (
            "question_scope",
            "raw_request_path",
            "raw_response_path",
            "raw_request_hash",
            "raw_response_hash",
            "provenance",
            "template_use",
            "session_id",
            "session_id_unavailable_reason",
        )
        for field in common_fields:
            self.assertIn(field, validation)
            self.assertEqual(validation[field], source[field])
        self.assertRegex(validation["raw_request_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(validation["raw_response_hash"], r"^[0-9a-f]{64}$")

    def test_prepared_run_validation_carries_semantic_integrity_fields(self) -> None:
        run_dir = self.prepare_fixture(SEMANTIC_FIXTURES[0])
        evidence = self.load_json(run_dir / "evidence.json")
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")
        persisted_validation = self.load_json(run_dir / "semantic_planner_validation.json")

        self.assert_validation_common_integrity_fields(
            persisted_validation,
            semantic_plan,
        )
        self.assert_release_ineligible_semantic_validation(
            persisted_validation,
            planner_mode=PLANNER_MODE_MANUAL_ANGLES,
        )

        computed_validation = semantic_planner_validation(
            run_dir=run_dir,
            evidence=evidence,
            tasks=[],
        )
        self.assert_validation_common_integrity_fields(
            computed_validation,
            semantic_plan,
        )
        self.assert_release_ineligible_semantic_validation(
            computed_validation,
            planner_mode=PLANNER_MODE_MANUAL_ANGLES,
        )

    def test_codex_semantic_validation_rejects_non_finite_fit_score(self) -> None:
        question = "How does the public transit fare cap affect downtown commuters?"
        cases = (
            ("nan", float("nan")),
            ("positive-infinity", float("inf")),
            ("negative-infinity", float("-inf")),
        )
        for case_name, score in cases:
            with self.subTest(case=case_name):
                run_dir = self.temp_runs_dir() / case_name
                self.write_json(
                    run_dir / "semantic_plan_review.json",
                    {
                        "semantic_fit_score": score,
                        "blockers": [],
                        "substitute_implementation_check": {"passed": True},
                    },
                )
                evidence = {
                    "run_id": case_name,
                    "question": question,
                    "semantic_planner": {
                        "question_class": "general",
                        "broad_question": False,
                        "planner_mode": "codex_semantic",
                        "semantic_release_eligible": True,
                        "expected_evidence_needs": ["primary_source"],
                    },
                    "semantic_angles": [
                        {
                            "angle_id": "angle_001",
                            "title": "Fare cap commuter impact",
                            "research_question": (
                                "How does the public transit fare cap affect downtown commuters?"
                            ),
                            "question_context": question,
                            "route": "text_only",
                            "evidence_need": "primary_source",
                            "expected_artifacts": ["official source list"],
                            "success_criteria": ["Tie commuter impact claims to source spans."],
                            "report_section": "Commuter Impact",
                        }
                    ],
                }

                validation = semantic_planner_validation(
                    run_dir=run_dir,
                    evidence=evidence,
                    tasks=[],
                )

                self.assertFalse(validation["ok"], validation)
                self.assertEqual(validation["planner_mode"], "codex_semantic")
                self.assertTrue(validation["semantic_release_eligible"])
                codes = {failure["code"] for failure in validation["failures"]}
                self.assertIn("semantic_fit_score_missing_or_non_finite", codes)

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

    def test_codex_semantic_raw_request_has_verbatim_input_without_class_menu(self) -> None:
        question = "Research lunar habitat material tests using official images and source records"
        result, adapter_request = self.prepare_with_codex_adapter(
            question,
            requirement_types=("subject", "source_quality", "visual_modality"),
            visual_angle_indexes=(3,),
        )
        run_dir = Path(result["run_dir"])
        raw_request = self.load_json(run_dir / "semantic_planner_raw" / "planner_request.json")
        request_text = json.dumps(raw_request, ensure_ascii=False).lower()

        self.assertEqual(result["status"], "awaiting_search_results")
        self.assertEqual(adapter_request["original_question"], question)
        self.assertEqual(raw_request["original_question"], question)
        self.assertEqual(raw_request["planner_mode"], PLANNER_MODE_CODEX_SEMANTIC)
        self.assertEqual(raw_request["adapter_request_hash"], adapter_request["adapter_request_hash"])
        self.assertFalse(raw_request["semantic_release_eligible"])
        forbidden = (
            "technical_api",
            "product_market",
            "visual_style",
            "policy_risk",
            "implementation_architecture",
            "official change inventory",
            "current architecture",
            "semantic schema design",
            "fan-out algorithm",
        )
        for token in forbidden:
            self.assertNotIn(token, request_text)

    def test_codex_semantic_accepts_structured_adapter_response_with_raw_provenance(self) -> None:
        question = "Research coastal microgrid outage recovery using official source records"
        result, adapter_request = self.prepare_with_codex_adapter(
            question,
            stdout_format="jsonl",
            requirement_types=("subject", "source_quality"),
        )
        run_dir = Path(result["run_dir"])
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")["semantic_plan"]
        raw_response = self.load_json(run_dir / "semantic_planner_raw" / "planner_response.json")

        self.assertEqual(semantic_plan["planner_mode"], PLANNER_MODE_CODEX_SEMANTIC)
        self.assertFalse(semantic_plan["semantic_release_eligible"])
        self.assertEqual(semantic_plan["question_scope"], "broad")
        self.assertGreaterEqual(len(semantic_plan["angles"]), 5)
        self.assertLessEqual(len(semantic_plan["angles"]), 8)
        self.assertGreaterEqual(len(semantic_plan["bounded_tasks"]), 20)
        self.assertLessEqual(len(semantic_plan["bounded_tasks"]), 40)

        tasks_per_angle: dict[str, int] = {}
        required_fields = {
            "task_id",
            "angle_id",
            "query",
            "route",
            "freshness_requirement",
            "source_policy",
            "expected_source_types",
            "expected_visual_targets",
            "expected_artifacts",
            "success_criteria",
            "max_sources",
            "max_images",
            "done_condition",
        }
        for task in semantic_plan["bounded_tasks"]:
            self.assertTrue(required_fields.issubset(task), task)
            tasks_per_angle[task["angle_id"]] = tasks_per_angle.get(task["angle_id"], 0) + 1
        for angle in semantic_plan["angles"]:
            self.assertGreaterEqual(tasks_per_angle.get(angle["angle_id"], 0), 2)
        self.assertEqual(
            raw_response["provenance"]["raw_request_hash"],
            adapter_request["adapter_request_hash"],
        )
        self.assertEqual(
            semantic_plan["planner_provenance"]["adapter_invocation"][
                "adapter_invocation_id"
            ],
            "adapter-test-invocation-001",
        )
        self.assertEqual(
            semantic_plan["planner_provenance"]["adapter_invocation"][
                "adapter_invocation_kind"
            ],
            CODEX_SEMANTIC_ADAPTER_INVOCATION_KIND,
        )
        self.assertEqual(
            semantic_plan["planner_provenance"]["adapter_invocation"]["codex_event_id"],
            "codex-event-002",
        )
        self.assertEqual(
            semantic_plan["planner_provenance"]["raw_response_hash"],
            raw_response["raw_response_hash"],
        )
        self.assertEqual(
            semantic_plan["planner_provenance"]["parsed_response_hash"],
            raw_response["parsed_response_hash"],
        )

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=semantic_plan,
        )
        self.assertTrue(validation["ok"], validation)

    def test_codex_semantic_unavailable_blocks_without_local_template_fallback(self) -> None:
        question = "Research adapter unavailable behavior without manual angles"
        result = prepare_run(question=question, runs_dir=self.temp_runs_dir())
        run_dir = Path(result["run_dir"])
        evidence = self.load_json(run_dir / "evidence.json")
        raw_request = self.load_json(run_dir / "semantic_planner_raw" / "planner_request.json")
        raw_response = self.load_json(run_dir / "semantic_planner_raw" / "planner_response.json")

        self.assertEqual(result["status"], "blocked_semantic_planner_unavailable")
        self.assertEqual(result["planner_mode"], PLANNER_MODE_BLOCKED)
        self.assertFalse(result["semantic_release_eligible"])
        self.assertEqual(
            evidence["semantic_planner"]["source"],
            "blocked_semantic_planner_unavailable",
        )
        self.assertEqual(evidence["semantic_angles"], [])
        self.assertEqual(evidence["search_tasks"], [])
        self.assertFalse((run_dir / "search_tasks.json").exists())
        self.assertEqual(raw_request["original_question"], question)
        self.assertEqual(raw_request["planner_mode"], PLANNER_MODE_CODEX_SEMANTIC)
        self.assertEqual(raw_response["status"], "blocked_semantic_planner_unavailable")
        self.assertEqual(raw_response["failure_category"], "adapter_unavailable")

    def test_codex_semantic_adapter_failure_blocks_without_template_fallback(self) -> None:
        def failing_adapter(_request: dict) -> dict:
            raise SemanticPlannerAdapterUnavailable("planner service unavailable")

        with mock.patch(
            "deepresearch.semantic_planner.invoke_codex_semantic_planner_adapter",
            side_effect=failing_adapter,
        ):
            result = prepare_run(
                question="Research adapter failure behavior",
                runs_dir=self.temp_runs_dir(),
            )
        run_dir = Path(result["run_dir"])
        raw_response = self.load_json(run_dir / "semantic_planner_raw" / "planner_response.json")

        self.assertEqual(result["status"], "blocked_semantic_planner_unavailable")
        self.assertEqual(result["planner_mode"], PLANNER_MODE_BLOCKED)
        self.assertEqual(raw_response["failure_category"], "adapter_unavailable")
        self.assertIn("planner service unavailable", raw_response["blocked_reason"])

    def test_codex_semantic_rejects_non_codex_adapter_command(self) -> None:
        with mock.patch(
            "deepresearch.semantic_planner.subprocess.run",
        ) as run_mock, mock.patch.dict(
            "os.environ",
            {CODEX_SEMANTIC_PLANNER_COMMAND_ENV: "python3 adapter.py --json"},
        ):
            result = prepare_run(
                question="Research invalid semantic adapter command",
                runs_dir=self.temp_runs_dir(),
            )
        run_mock.assert_not_called()
        raw_response = self.load_json(
            Path(result["run_dir"]) / "semantic_planner_raw" / "planner_response.json"
        )

        self.assertEqual(result["status"], "blocked_semantic_planner_unavailable")
        self.assertEqual(raw_response["failure_category"], "adapter_unavailable")
        self.assertIn("must use codex exec --json", raw_response["blocked_reason"])

    def test_codex_semantic_rejects_codex_exec_without_json_mode(self) -> None:
        with mock.patch(
            "deepresearch.semantic_planner.subprocess.run",
        ) as run_mock, mock.patch.dict(
            "os.environ",
            {CODEX_SEMANTIC_PLANNER_COMMAND_ENV: "codex exec"},
        ):
            result = prepare_run(
                question="Research semantic adapter command without JSON mode",
                runs_dir=self.temp_runs_dir(),
            )
        run_mock.assert_not_called()
        raw_response = self.load_json(
            Path(result["run_dir"]) / "semantic_planner_raw" / "planner_response.json"
        )

        self.assertEqual(result["status"], "blocked_semantic_planner_unavailable")
        self.assertEqual(raw_response["failure_category"], "adapter_unavailable")
        self.assertIn("must enable JSON mode", raw_response["blocked_reason"])

    def test_codex_semantic_rejects_response_without_codex_raw_identity(self) -> None:
        def strip_codex_identity(response: dict) -> dict:
            response = json.loads(json.dumps(response))
            response.pop("provenance", None)
            return response

        result, _adapter_request = self.prepare_with_codex_adapter(
            "Research missing Codex response provenance",
            response_mutator=strip_codex_identity,
        )
        raw_response = self.load_json(
            Path(result["run_dir"]) / "semantic_planner_raw" / "planner_response.json"
        )

        self.assertEqual(result["status"], "blocked_semantic_planner_unavailable")
        self.assertEqual(raw_response["failure_category"], "adapter_invalid_response")
        self.assertIn("lacks Codex raw response identity", raw_response["blocked_reason"])

    def test_adapter_command_alone_is_not_valid_codex_semantic_provenance(self) -> None:
        with self.assertRaisesRegex(
            SemanticPlannerAdapterUnavailable,
            "adapter_invocation_kind",
        ):
            validate_codex_semantic_adapter_provenance(
                raw_request={"adapter_request_hash": "request-hash"},
                provenance={
                    "adapter_command": ["codex", "exec", "--json"],
                    "raw_request_hash": "request-hash",
                },
            )

    def test_codex_semantic_bounded_tasks_are_preserved_into_research_tasks(self) -> None:
        question = "Research public water system resilience with official records"
        result, _adapter_request = self.prepare_with_codex_adapter(question)
        run_dir = Path(result["run_dir"])
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")["semantic_plan"]
        search_tasks = self.load_json(run_dir / "search_tasks.json")["tasks"]

        self.assertEqual(len(search_tasks), len(semantic_plan["bounded_tasks"]))
        first_bounded = semantic_plan["bounded_tasks"][0]
        first_search = search_tasks[0]
        for field in (
            "task_id",
            "angle_id",
            "query",
            "route",
            "freshness_requirement",
            "source_policy",
            "expected_source_types",
            "expected_visual_targets",
            "expected_artifacts",
            "success_criteria",
            "max_sources",
            "max_images",
            "done_condition",
        ):
            self.assertEqual(first_search[field], first_bounded[field], field)

        planned = plan_research_tasks(run=run_dir, min_tasks=1)
        research_tasks = planned["tasks"]
        self.assertEqual(len(research_tasks), len(semantic_plan["bounded_tasks"]))
        first_research = research_tasks[0]
        for field in (
            "task_id",
            "angle_id",
            "query",
            "route",
            "freshness_requirement",
            "source_policy",
            "expected_source_types",
            "expected_visual_targets",
            "expected_artifacts",
            "success_criteria",
            "max_sources",
            "max_images",
            "done_condition",
        ):
            self.assertEqual(first_research[field], first_bounded[field], field)

        second_result, _adapter_request = self.prepare_with_codex_adapter(
            "Research public water system resilience with official records"
        )
        with self.assertRaisesRegex(ParallelOrchestrationError, "cannot be truncated"):
            plan_research_tasks(run=Path(second_result["run_dir"]), min_tasks=1, max_tasks=1)

    def test_plan_research_tasks_rejects_stale_semantic_search_tasks(self) -> None:
        result, _adapter_request = self.prepare_with_codex_adapter(
            "Research semantic search task alignment"
        )
        run_dir = Path(result["run_dir"])
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["search_tasks"][0]["query"] = "stale query from an old materialization"
        self.write_json(run_dir / "evidence.json", evidence)

        with self.assertRaisesRegex(
            ParallelOrchestrationError,
            r"search_tasks.*query.*semantic_planner\.bounded_tasks",
        ):
            plan_research_tasks(run=run_dir, min_tasks=1)

    def test_plan_research_tasks_rejects_stale_existing_research_tasks(self) -> None:
        result, _adapter_request = self.prepare_with_codex_adapter(
            "Research semantic research task alignment"
        )
        run_dir = Path(result["run_dir"])
        plan_research_tasks(run=run_dir, min_tasks=1)
        research_tasks = self.load_json(run_dir / "research_tasks.json")
        research_tasks["tasks"][0]["done_condition"] = "stale completion condition"
        self.write_json(run_dir / "research_tasks.json", research_tasks)

        with self.assertRaisesRegex(
            ParallelOrchestrationError,
            r"research_tasks.*done_condition.*semantic_planner\.bounded_tasks",
        ):
            plan_research_tasks(run=run_dir, min_tasks=1)

    def test_semantically_plausible_wrong_candidate_plans_fail_validation(self) -> None:
        question = (
            "2026 Korea battery safety image evidence from official sources as a table"
        )
        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            requirement_types=(
                "subject",
                "visual_modality",
                "source_quality",
                "time_range",
                "geography",
                "deliverable_shape",
            ),
            visual_angle_indexes=(3,),
        )
        run_dir = Path(result["run_dir"])
        plan = self.load_json(run_dir / "semantic_plan.json")["semantic_plan"]

        positive_validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=plan,
        )
        self.assertTrue(positive_validation["ok"], positive_validation)

        no_visual = json.loads(json.dumps(plan))
        for angle in no_visual["angles"]:
            angle["route"] = "text_only"
            angle["expected_visual_targets"] = []
        for task in no_visual["bounded_tasks"]:
            task["route"] = "text_only"
            task["expected_visual_targets"] = []
            task["max_images"] = 0
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=no_visual,
        )
        self.assertFalse(validation["ok"])
        self.assertIn(
            "visual_requirement_missing_visual_route",
            {failure["code"] for failure in validation["failures"]},
        )

        no_official = json.loads(json.dumps(plan))
        for task in no_official["bounded_tasks"]:
            task["source_policy"] = {"decision": "allowed", "flags": []}
            task["expected_source_types"] = ["blog posts", "forum discussions"]
            task["success_criteria"] = [
                criterion
                for criterion in task["success_criteria"]
                if "official" not in criterion.lower()
                and "regulatory" not in criterion.lower()
                and "primary" not in criterion.lower()
            ]
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=no_official,
        )
        self.assertFalse(validation["ok"])
        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertIn("official_source_requirement_missing", failure_codes)
        self.assertIn("official_source_success_criteria_missing", failure_codes)

        wrong_subject = json.loads(json.dumps(plan))
        for angle in wrong_subject["angles"]:
            angle["title"] = "Codex runner architecture"
            angle["research_question"] = "How should Codex runner internals be implemented?"
            angle["why_this_angle_matters"] = "This generic implementation angle is out of scope."
            angle["search_queries"] = ["Codex runner architecture implementation"]
            angle["included_scope"] = ["Codex implementation"]
            angle["expected_source_types"] = ["repository implementation notes"]
            angle["expected_visual_targets"] = []
            angle["expected_artifacts"] = ["implementation checklist"]
            angle["success_criteria"] = ["Findings must explain Codex runner internals."]
        for task in wrong_subject["bounded_tasks"]:
            task["query"] = "Codex runner architecture implementation and test strategy"
            task["success_criteria"] = ["Findings must explain Codex runner internals."]
            task["expected_artifacts"] = ["implementation checklist"]
            task["expected_source_types"] = ["repository implementation notes"]
            task["expected_visual_targets"] = []
            task["done_condition"] = "Stop after documenting Codex runner implementation details."
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=wrong_subject,
        )
        self.assertFalse(validation["ok"])
        self.assertIn(
            "subject_requirement_drift",
            {failure["code"] for failure in validation["failures"]},
        )

        no_time = json.loads(json.dumps(plan))
        for task in no_time["bounded_tasks"]:
            task["freshness_requirement"] = "any"
            task["query"] = task["query"].replace("2026", "historical")
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=no_time,
        )
        self.assertFalse(validation["ok"])
        self.assertIn(
            "time_requirement_missing_recent_task",
            {failure["code"] for failure in validation["failures"]},
        )

        no_geography = json.loads(json.dumps(plan))
        for task in no_geography["bounded_tasks"]:
            task["query"] = task["query"].replace("Korea", "global")
            task["expected_source_types"] = [
                source_type.replace("local", "global").replace("regional", "global")
                for source_type in task["expected_source_types"]
            ]
            task["success_criteria"] = [
                criterion.replace("Korea", "global")
                for criterion in task["success_criteria"]
            ]
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=no_geography,
        )
        self.assertFalse(validation["ok"])
        self.assertIn(
            "geography_requirement_missing_task_scope",
            {failure["code"] for failure in validation["failures"]},
        )

        no_table_output = json.loads(json.dumps(plan))
        for task in no_table_output["bounded_tasks"]:
            task["expected_artifacts"] = [
                artifact
                for artifact in task["expected_artifacts"]
                if "table" not in artifact.lower()
                and "matrix" not in artifact.lower()
                and "표" not in artifact
            ]
            task["success_criteria"] = [
                criterion
                for criterion in task["success_criteria"]
                if "table" not in criterion.lower()
                and "matrix" not in criterion.lower()
                and "표" not in criterion
            ]
            task["done_condition"] = "Stop when findings have source-backed caveats."
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=no_table_output,
        )
        self.assertFalse(validation["ok"])
        self.assertIn(
            "deliverable_requirement_missing_task_output",
            {failure["code"] for failure in validation["failures"]},
        )

    def test_visual_route_override_without_angles_blocks_when_adapter_unavailable(self) -> None:
        for route in ("visual_required", "visual_optional"):
            with self.subTest(route=route):
                result = prepare_run(
                    question="Find image evidence for a public product interface",
                    runs_dir=self.temp_runs_dir(),
                    route=route,
                    budget_preset="standard",
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
                self.assertEqual(
                    evidence["semantic_planner"]["status"],
                    "blocked_semantic_planner_unavailable",
                )
                self.assertEqual(evidence["semantic_angles"], [])
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

    def test_korean_visual_official_prompt_uses_adapter_output_not_local_inventory(self) -> None:
        question = "전기차 배터리 화재 열폭주 안전 테스트 이미지와 규제 근거를 조사해줘"
        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            requirement_types=("subject", "visual_modality", "source_quality"),
            visual_angle_indexes=(3,),
        )
        run_dir = Path(result["run_dir"])
        evidence = self.load_json(run_dir / "evidence.json")
        validation = self.load_json(run_dir / "semantic_planner_validation.json")
        review = self.load_json(run_dir / "semantic_plan_review.json")
        plan_artifact = self.load_json(run_dir / "semantic_plan.json")
        semantic_plan = plan_artifact["semantic_plan"]

        self.assertEqual(evidence["semantic_planner"]["planner_mode"], PLANNER_MODE_CODEX_SEMANTIC)
        self.assertFalse(evidence["semantic_planner"]["semantic_release_eligible"])
        self.assert_codex_candidate_release_ineligible(validation)
        self.assertEqual(
            evidence["semantic_planner"]["status"],
            "candidate_codex_semantic_release_ineligible",
        )
        text = json.dumps(semantic_plan, ensure_ascii=False).lower()
        self.assertIn(question.lower(), text)
        self.assertIn("official", text)
        self.assertIn("visual", text)
        bounded_task_text = json.dumps(
            semantic_plan["bounded_tasks"],
            ensure_ascii=False,
        ).lower()
        for token in ("전기차", "배터리", "화재", "열폭주", "규제", "이미지"):
            self.assertIn(token, bounded_task_text)
        search_tasks = self.load_json(run_dir / "search_tasks.json")["tasks"]
        search_task_text = json.dumps(search_tasks, ensure_ascii=False).lower()
        for token in ("전기차", "배터리", "화재", "열폭주", "규제", "이미지"):
            self.assertIn(token, search_task_text)
        source_type_text = json.dumps(
            [task["expected_source_types"] for task in semantic_plan["bounded_tasks"]],
            ensure_ascii=False,
        ).lower()
        self.assertIn("official", source_type_text)
        self.assertIn("regulatory", source_type_text)
        visual_target_text = json.dumps(
            [task["expected_visual_targets"] for task in semantic_plan["bounded_tasks"]],
            ensure_ascii=False,
        ).lower()
        for token in ("전기차", "배터리", "화재", "열폭주", "이미지"):
            self.assertIn(token, visual_target_text)
        generated_work = json.dumps(
            {
                "angle_work": [
                    {
                        "title": angle["title"],
                        "research_question": angle["research_question"],
                        "search_queries": angle["search_queries"],
                    }
                    for angle in semantic_plan["angles"]
                ],
                "bounded_task_queries": [
                    task["query"] for task in semantic_plan["bounded_tasks"]
                ],
            },
            ensure_ascii=False,
        ).lower()
        self.assertNotIn("local deterministic template", generated_work)
        self.assertTrue(
            any(angle["route"] != "text_only" for angle in semantic_plan["angles"])
        )
        candidate_validation = validate_semantic_candidate_plan(
            original_question=evidence["question"],
            plan=semantic_plan,
        )
        self.assertTrue(candidate_validation["ok"], candidate_validation)
        self.assertFalse(review["substitute_implementation_check"]["passed"])
        self.assertNotEqual(review.get("semantic_fit_score"), SEMANTIC_FIT_SCORE_THRESHOLD)

    def test_codex_deepresearch_planner_architecture_prompt_uses_codex_semantic_candidate_tasks(self) -> None:
        question = (
            "Codex DeepResearch semantic planner 아키텍처와 테스트 전략을 "
            "구현 관점에서 조사해줘"
        )
        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            requirement_types=("subject",),
        )
        run_dir = Path(result["run_dir"])
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")["semantic_plan"]
        raw_response = self.load_json(run_dir / "semantic_planner_raw" / "planner_response.json")

        self.assertEqual(result["semantic_planning_status"], "candidate_codex_semantic_release_ineligible")
        self.assertEqual(semantic_plan["planner_mode"], PLANNER_MODE_CODEX_SEMANTIC)
        self.assertFalse(semantic_plan["semantic_release_eligible"])
        self.assertEqual(
            raw_response["provenance"]["adapter_invocation_kind"],
            CODEX_SEMANTIC_ADAPTER_INVOCATION_KIND,
        )
        bounded_task_text = json.dumps(
            semantic_plan["bounded_tasks"],
            ensure_ascii=False,
        ).lower()
        for token in (
            "codex",
            "deepresearch",
            "semantic planner",
            "아키텍처",
            "테스트 전략",
            "구현",
        ):
            self.assertIn(token, bounded_task_text)
        self.assertNotIn("local deterministic template", bounded_task_text)
        candidate_validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=semantic_plan,
        )
        self.assertTrue(candidate_validation["ok"], candidate_validation)


if __name__ == "__main__":
    unittest.main()
