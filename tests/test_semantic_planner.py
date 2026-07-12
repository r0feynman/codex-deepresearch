from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
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
    CODEX_SEMANTIC_ADAPTER_WORKDIR_ENV,
    CODEX_SEMANTIC_ENABLE_DEFAULT_ADAPTER_ENV,
    CODEX_SEMANTIC_ORACLE_COMMAND_ENV,
    CODEX_SEMANTIC_PLANNER_COMMAND_ENV,
    CODEX_SEMANTIC_PLANNER_VALIDATION_MAX_ATTEMPTS_ENV,
    CODEX_SEMANTIC_REVIEWER_COMMAND_ENV,
    PLANNER_MODE_BLOCKED,
    PLANNER_MODE_CODEX_SEMANTIC,
    PLANNER_MODE_FIXTURE,
    PLANNER_MODE_HEURISTIC_TEMPLATE_FALLBACK,
    PLANNER_MODE_MANUAL_ANGLES,
    SEMANTIC_FIT_SCORE_THRESHOLD,
    SemanticAngle,
    SemanticPlan,
    SEMANTIC_MATERIALIZATION_ALIGNMENT_FIELDS,
    SEMANTIC_MATERIALIZATION_SEARCH_RESULT_FIELD_PREFIX,
    SemanticPlannerAdapterUnavailable,
    build_codex_semantic_raw_request,
    build_semantic_materialization_diff,
    heuristic_template_planner,
    question_mentions_visual_evidence,
    semantic_planner_validation,
    validate_semantic_candidate_plan,
    validate_codex_semantic_adapter_provenance,
    write_semantic_materialization_diff,
    write_semantic_planner_validation,
    _codex_semantic_planner_validation_max_attempts,
    _has_forbidden_internal_leakage,
    _semantic_adapter_command,
    _semantic_adapter_prompt,
    _semantic_substitute_implementation_check,
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
    def setUp(self) -> None:
        codex_path_patch = mock.patch(
            "deepresearch.semantic_planner.shutil.which",
            return_value=None,
        )
        codex_path_patch.start()
        self.addCleanup(codex_path_patch.stop)

    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def load_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def write_jsonl(self, path: Path, records: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(
                json.dumps(record, sort_keys=True) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )

    def read_jsonl(self, path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def parse_timestamp(self, value: str) -> datetime:
        raw = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def write_semantic_materialization_fixture(self, *, visual: bool = True) -> Path:
        run_dir = self.temp_runs_dir() / "materialization"
        run_dir.mkdir(parents=True)
        route = "visual_required" if visual else "text_only"
        tasks = [
            {
                "task_id": f"task_search_{index:03d}",
                "angle_id": f"angle_{index:03d}",
                "query": f"semantic materialization task {index}",
                "route": route,
                "freshness_requirement": "any",
                "source_policy": {"decision": "allowed", "flags": []},
                "expected_source_types": ["web"],
                "expected_visual_targets": [f"visual target {index}"] if visual else [],
                "expected_artifacts": ["source list"],
                "success_criteria": ["Claims are source linked."],
                "done_condition": "At least one supported claim is available.",
                "max_results": 8,
                "max_sources": 3,
                "max_images": 2 if visual else 0,
            }
            for index in range(1, 4)
        ]
        self.write_json(
            run_dir / "semantic_plan.json",
            {
                "schema_version": "codex-deepresearch.semantic-planner.v0",
                "artifact_type": "semantic_plan",
                "semantic_plan": {"bounded_tasks": tasks},
            },
        )
        semantic_plan_hash = hashlib.sha256(
            (run_dir / "semantic_plan.json").read_bytes()
        ).hexdigest()
        search_tasks = [
            {
                "id": task["task_id"],
                "semantic_plan_task_id": task["task_id"],
                "semantic_plan_hash": semantic_plan_hash,
                "approved_delta_id": "base_plan",
                **task,
            }
            for task in tasks
        ]
        research_tasks = [
            {**task, "state": "merged", "output_shard_path": f"evidence_shards/{task['task_id']}/evidence_shard.json"}
            for task in search_tasks
        ]
        visual_tasks = [
            {**task, "visual_tasks": task["expected_visual_targets"], "status": "planned"}
            for task in search_tasks
            if task["route"] != "text_only"
        ]
        self.write_json(run_dir / "search_tasks.json", {"tasks": search_tasks})
        self.write_json(run_dir / "research_tasks.json", {"tasks": research_tasks})
        self.write_json(run_dir / "visual_tasks.json", {"tasks": visual_tasks})
        self.write_json(
            run_dir / "visual_search_plan.json",
            {"tasks": visual_tasks},
        )
        self.write_json(
            run_dir / "evidence.json",
            {
                "search_tasks": search_tasks,
                "images": [
                    {
                        "id": f"img_{index:03d}",
                        "task_id": task["task_id"],
                        "semantic_plan_task_id": task["task_id"],
                        "semantic_plan_hash": semantic_plan_hash,
                        "angle_id": task["angle_id"],
                        "route": task["route"],
                        "approved_delta_id": "base_plan",
                    }
                    for index, task in enumerate(visual_tasks, start=1)
                ],
            },
        )
        self.write_jsonl(
            run_dir / "search_results.jsonl",
            [
                {
                    "id": f"result_{index:03d}",
                    "task_id": task["task_id"],
                    "semantic_plan_task_id": task["task_id"],
                    "semantic_plan_hash": semantic_plan_hash,
                    "angle_id": task["angle_id"],
                    "route": task["route"],
                    "query": task["query"],
                    "freshness_requirement": task["freshness_requirement"],
                    "source_policy": task["source_policy"],
                    **{
                        f"{SEMANTIC_MATERIALIZATION_SEARCH_RESULT_FIELD_PREFIX}{field}": task.get(field)
                        for field in SEMANTIC_MATERIALIZATION_ALIGNMENT_FIELDS
                    },
                    "approved_delta_id": "base_plan",
                }
                for index, task in enumerate(search_tasks, start=1)
            ],
        )
        self.write_jsonl(
            run_dir / "subagent_assignments.jsonl",
            [
                {
                    "assignment_id": f"assign_{index:03d}",
                    "task_id": task["task_id"],
                    "semantic_plan_task_id": task["task_id"],
                    "semantic_plan_hash": semantic_plan_hash,
                    "angle_id": task["angle_id"],
                    "route": task["route"],
                    "approved_delta_id": "base_plan",
                }
                for index, task in enumerate(search_tasks, start=1)
            ],
        )
        self.write_jsonl(
            run_dir / "visual_candidates.jsonl",
            [
                {
                    "candidate_id": f"cand_{index:03d}",
                    "task_id": task["task_id"],
                    "semantic_plan_task_id": task["task_id"],
                    "semantic_plan_hash": semantic_plan_hash,
                    "angle_id": task["angle_id"],
                    "route": task["route"],
                    "approved_delta_id": "base_plan",
                }
                for index, task in enumerate(visual_tasks, start=1)
            ],
        )
        self.write_jsonl(
            run_dir / "image_fetch_status.jsonl",
            [
                {
                    "fetch_id": f"fetch_{index:03d}",
                    "task_id": task["task_id"],
                    "semantic_plan_task_id": task["task_id"],
                    "semantic_plan_hash": semantic_plan_hash,
                    "angle_id": task["angle_id"],
                    "route": task["route"],
                    "approved_delta_id": "base_plan",
                }
                for index, task in enumerate(visual_tasks, start=1)
            ],
        )
        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            [
                {
                    "observation_id": f"obs_{index:03d}",
                    "task_id": task["task_id"],
                    "semantic_plan_task_id": task["task_id"],
                    "semantic_plan_hash": semantic_plan_hash,
                    "angle_id": task["angle_id"],
                    "route": task["route"],
                    "approved_delta_id": "base_plan",
                }
                for index, task in enumerate(visual_tasks, start=1)
            ],
        )
        return run_dir

    def realign_semantic_materialization_fixture(self, run_dir: Path) -> tuple[str, list[dict]]:
        plan = self.load_json(run_dir / "semantic_plan.json")
        tasks = plan["semantic_plan"]["bounded_tasks"]
        semantic_plan_hash = hashlib.sha256(
            (run_dir / "semantic_plan.json").read_bytes()
        ).hexdigest()
        task_by_id = {task["task_id"]: task for task in tasks}

        def record_task_id(record: dict) -> str:
            return str(
                record.get("semantic_plan_task_id")
                or record.get("task_id")
                or record.get("search_task_id")
                or record.get("id")
                or ""
            )

        def align_record(record: dict) -> dict:
            task = task_by_id.get(record_task_id(record))
            record["semantic_plan_hash"] = semantic_plan_hash
            if task is None:
                return record
            record["semantic_plan_task_id"] = task["task_id"]
            record.setdefault("approved_delta_id", "base_plan")
            for field in SEMANTIC_MATERIALIZATION_ALIGNMENT_FIELDS:
                if field in task:
                    record[field] = task[field]
            return record

        for artifact_name in (
            "search_tasks.json",
            "research_tasks.json",
            "visual_tasks.json",
            "visual_search_plan.json",
        ):
            path = run_dir / artifact_name
            if not path.exists():
                continue
            payload = self.load_json(path)
            payload["tasks"] = [align_record(dict(record)) for record in payload["tasks"]]
            self.write_json(path, payload)

        evidence = self.load_json(run_dir / "evidence.json")
        evidence["search_tasks"] = [
            align_record(dict(record)) for record in evidence["search_tasks"]
        ]
        evidence["images"] = [
            align_record(dict(record)) for record in evidence.get("images", [])
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        search_results = []
        for record in self.read_jsonl(run_dir / "search_results.jsonl"):
            task = task_by_id[record["semantic_plan_task_id"]]
            record["semantic_plan_hash"] = semantic_plan_hash
            for field in SEMANTIC_MATERIALIZATION_ALIGNMENT_FIELDS:
                record[f"{SEMANTIC_MATERIALIZATION_SEARCH_RESULT_FIELD_PREFIX}{field}"] = task.get(field)
            search_results.append(record)
        self.write_jsonl(run_dir / "search_results.jsonl", search_results)

        for artifact_name in (
            "subagent_assignments.jsonl",
            "visual_candidates.jsonl",
            "image_fetch_status.jsonl",
            "visual_observations.jsonl",
        ):
            path = run_dir / artifact_name
            if not path.exists():
                continue
            records = [align_record(dict(record)) for record in self.read_jsonl(path)]
            self.write_jsonl(path, records)

        return semantic_plan_hash, tasks

    def drop_visual_materialization_records(
        self,
        run_dir: Path,
        task_ids: set[str],
    ) -> None:
        for artifact_name in ("visual_tasks.json", "visual_search_plan.json"):
            payload = self.load_json(run_dir / artifact_name)
            payload["tasks"] = [
                record
                for record in payload["tasks"]
                if record.get("semantic_plan_task_id") not in task_ids
                and record.get("task_id") not in task_ids
            ]
            self.write_json(run_dir / artifact_name, payload)

        evidence = self.load_json(run_dir / "evidence.json")
        evidence["images"] = [
            record
            for record in evidence.get("images", [])
            if record.get("semantic_plan_task_id") not in task_ids
            and record.get("task_id") not in task_ids
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        for artifact_name in (
            "visual_candidates.jsonl",
            "image_fetch_status.jsonl",
            "visual_observations.jsonl",
        ):
            records = [
                record
                for record in self.read_jsonl(run_dir / artifact_name)
                if record.get("semantic_plan_task_id") not in task_ids
                and record.get("task_id") not in task_ids
            ]
            self.write_jsonl(run_dir / artifact_name, records)

    def test_semantic_materialization_diff_validates_exact_task_sets(self) -> None:
        run_dir = self.write_semantic_materialization_fixture()
        diff = write_semantic_materialization_diff(
            run_dir=run_dir,
            require_research_tasks=True,
            require_downstream=True,
        )

        self.assertTrue(diff["valid"], diff)
        self.assertTrue(diff["full_materialization_validation_implemented"])
        self.assertEqual(diff["missing_task_ids"], [])
        self.assertEqual(diff["extra_task_ids"], [])
        self.assertEqual(diff["duplicate_semantic_task_ids"], [])
        self.assertEqual(diff["dropped_search_obligations"], [])
        self.assertEqual(diff["dropped_visual_obligations"], [])

    def test_semantic_materialization_diff_excludes_zero_image_visual_helpers_from_visual_obligations(self) -> None:
        run_dir = self.write_semantic_materialization_fixture()
        plan = self.load_json(run_dir / "semantic_plan.json")
        tasks = plan["semantic_plan"]["bounded_tasks"]
        for task in tasks[1:]:
            task["route"] = "visual_optional"
            task["expected_visual_targets"] = []
            task["expected_artifacts"] = ["source list"]
            task["max_images"] = 0
        self.write_json(run_dir / "semantic_plan.json", plan)
        semantic_plan_hash = hashlib.sha256(
            (run_dir / "semantic_plan.json").read_bytes()
        ).hexdigest()
        task_by_id = {task["task_id"]: task for task in tasks}

        def align_record(record: dict) -> dict:
            task_id = record.get("semantic_plan_task_id") or record.get("task_id") or record.get("id")
            task = task_by_id.get(task_id)
            record["semantic_plan_hash"] = semantic_plan_hash
            if task is None:
                return record
            for field in SEMANTIC_MATERIALIZATION_ALIGNMENT_FIELDS:
                if field in task:
                    record[field] = task[field]
            return record

        for artifact_name in ("search_tasks.json", "research_tasks.json"):
            payload = self.load_json(run_dir / artifact_name)
            payload["tasks"] = [align_record(dict(record)) for record in payload["tasks"]]
            self.write_json(run_dir / artifact_name, payload)

        visual_payload = self.load_json(run_dir / "visual_tasks.json")
        visual_payload["tasks"] = [
            align_record(dict(record))
            for record in visual_payload["tasks"]
            if record.get("task_id") == "task_search_001"
        ]
        self.write_json(run_dir / "visual_tasks.json", visual_payload)
        self.write_json(run_dir / "visual_search_plan.json", visual_payload)

        evidence = self.load_json(run_dir / "evidence.json")
        evidence["search_tasks"] = [
            align_record(dict(record)) for record in evidence["search_tasks"]
        ]
        evidence["images"] = [
            {
                **image,
                "semantic_plan_hash": semantic_plan_hash,
            }
            for image in evidence["images"]
            if image.get("semantic_plan_task_id") == "task_search_001"
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        search_results = []
        for record in self.read_jsonl(run_dir / "search_results.jsonl"):
            task = task_by_id[record["semantic_plan_task_id"]]
            record["semantic_plan_hash"] = semantic_plan_hash
            for field in SEMANTIC_MATERIALIZATION_ALIGNMENT_FIELDS:
                record[f"{SEMANTIC_MATERIALIZATION_SEARCH_RESULT_FIELD_PREFIX}{field}"] = task.get(field)
            search_results.append(record)
        self.write_jsonl(run_dir / "search_results.jsonl", search_results)

        for artifact_name in (
            "subagent_assignments.jsonl",
            "visual_candidates.jsonl",
            "image_fetch_status.jsonl",
            "visual_observations.jsonl",
        ):
            records = []
            for record in self.read_jsonl(run_dir / artifact_name):
                if artifact_name != "subagent_assignments.jsonl" and record.get("semantic_plan_task_id") != "task_search_001":
                    continue
                record["semantic_plan_hash"] = semantic_plan_hash
                records.append(record)
            self.write_jsonl(run_dir / artifact_name, records)

        diff = build_semantic_materialization_diff(
            run_dir=run_dir,
            require_research_tasks=True,
            require_downstream=True,
        )

        self.assertTrue(diff["valid"], diff)
        self.assertEqual(diff["visual_obligation_task_ids"], ["task_search_001"])
        self.assertEqual(diff["missing_task_ids"], [])
        self.assertEqual(diff["dropped_visual_obligations"], [])
        visual_artifacts = {
            check["artifact"]: check["planned_task_ids"]
            for check in diff["artifact_checks"]
            if check["artifact"]
            in {
                "visual_tasks",
                "visual_search_plan",
                "visual_candidates",
                "image_fetch_status",
                "visual_observations",
                "evidence.images",
            }
        }
        self.assertEqual(
            visual_artifacts,
            {
                "visual_tasks": ["task_search_001"],
                "visual_search_plan": ["task_search_001"],
                "visual_candidates": ["task_search_001"],
                "image_fetch_status": ["task_search_001"],
                "visual_observations": ["task_search_001"],
                "evidence.images": ["task_search_001"],
            },
        )

    def test_semantic_materialization_diff_keeps_zero_image_target_visual_obligation(self) -> None:
        run_dir = self.write_semantic_materialization_fixture()
        plan = self.load_json(run_dir / "semantic_plan.json")
        task = plan["semantic_plan"]["bounded_tasks"][1]
        task["route"] = "visual_required"
        task["expected_visual_targets"] = ["representative image evidence"]
        task["expected_artifacts"] = ["source list"]
        task["max_images"] = 0
        self.write_json(run_dir / "semantic_plan.json", plan)
        self.realign_semantic_materialization_fixture(run_dir)
        self.drop_visual_materialization_records(run_dir, {"task_search_002"})

        diff = build_semantic_materialization_diff(
            run_dir=run_dir,
            require_research_tasks=True,
            require_downstream=True,
        )

        self.assertFalse(diff["valid"], diff)
        self.assertIn("task_search_002", diff["visual_obligation_task_ids"])
        self.assertIn("task_search_002", diff["missing_task_ids"])
        self.assertIn("task_search_002", diff["dropped_visual_obligations"])

    def test_semantic_materialization_diff_keeps_zero_image_expected_evidence_visual_obligation(self) -> None:
        run_dir = self.write_semantic_materialization_fixture()
        plan = self.load_json(run_dir / "semantic_plan.json")
        task = plan["semantic_plan"]["bounded_tasks"][1]
        task["route"] = "visual_required"
        task["expected_visual_targets"] = []
        task["expected_artifacts"] = ["source list"]
        task["expected_evidence"] = ["visual_observation", "vlm_analysis"]
        task["max_images"] = 0
        self.write_json(run_dir / "semantic_plan.json", plan)
        self.realign_semantic_materialization_fixture(run_dir)
        self.drop_visual_materialization_records(run_dir, {"task_search_002"})

        diff = build_semantic_materialization_diff(
            run_dir=run_dir,
            require_research_tasks=True,
            require_downstream=True,
        )

        self.assertFalse(diff["valid"], diff)
        self.assertIn("task_search_002", diff["visual_obligation_task_ids"])
        self.assertIn("task_search_002", diff["missing_task_ids"])
        self.assertIn("task_search_002", diff["dropped_visual_obligations"])

    def test_semantic_materialization_diff_keeps_positive_image_visual_required_obligation(self) -> None:
        run_dir = self.write_semantic_materialization_fixture()
        plan = self.load_json(run_dir / "semantic_plan.json")
        task = plan["semantic_plan"]["bounded_tasks"][1]
        task["route"] = "visual_required"
        task["expected_visual_targets"] = []
        task["expected_artifacts"] = ["source list"]
        task["expected_evidence"] = ["primary_source"]
        task["max_images"] = 2
        self.write_json(run_dir / "semantic_plan.json", plan)
        self.realign_semantic_materialization_fixture(run_dir)
        self.drop_visual_materialization_records(run_dir, {"task_search_002"})

        diff = build_semantic_materialization_diff(
            run_dir=run_dir,
            require_research_tasks=True,
            require_downstream=True,
        )

        self.assertFalse(diff["valid"], diff)
        self.assertIn("task_search_002", diff["visual_obligation_task_ids"])
        self.assertIn("task_search_002", diff["missing_task_ids"])
        self.assertIn("task_search_002", diff["dropped_visual_obligations"])

    def test_semantic_materialization_diff_allows_executed_search_query_to_differ_from_semantic_lineage(self) -> None:
        run_dir = self.write_semantic_materialization_fixture(visual=False)
        records = self.read_jsonl(run_dir / "search_results.jsonl")
        records[0]["query"] = "provider executed a narrower web search query"
        records[0]["freshness_requirement"] = "latest"
        self.write_jsonl(run_dir / "search_results.jsonl", records)

        diff = build_semantic_materialization_diff(
            run_dir=run_dir,
            require_research_tasks=True,
            require_downstream=True,
        )

        self.assertTrue(diff["valid"], diff)
        search_check = next(
            check for check in diff["artifact_checks"] if check["artifact"] == "search_results"
        )
        self.assertIn("semantic_task_query", search_check["compared_fields"])
        self.assertIn(
            "semantic_task_freshness_requirement",
            search_check["compared_fields"],
        )
        self.assertFalse(
            [
                mismatch
                for mismatch in diff["field_mismatches"]
                if mismatch["artifact"] == "search_results"
            ],
            diff,
        )

    def test_semantic_materialization_diff_rejects_missing_or_mismatched_search_result_semantic_lineage(self) -> None:
        cases = (
            ("missing_query", "semantic_task_query", "field_missing"),
            (
                "mismatched_freshness",
                "semantic_task_freshness_requirement",
                "field_mismatch",
            ),
        )
        for case_name, expected_field, expected_code in cases:
            with self.subTest(case_name=case_name):
                run_dir = self.write_semantic_materialization_fixture(visual=False)
                records = self.read_jsonl(run_dir / "search_results.jsonl")
                if case_name == "missing_query":
                    records[0].pop("semantic_task_query")
                else:
                    records[0]["semantic_task_freshness_requirement"] = "wrong semantic freshness"
                self.write_jsonl(run_dir / "search_results.jsonl", records)

                diff = build_semantic_materialization_diff(
                    run_dir=run_dir,
                    require_research_tasks=True,
                    require_downstream=True,
                )

                self.assertFalse(diff["valid"], diff)
                self.assertTrue(
                    any(
                        mismatch["artifact"] == "search_results"
                        and mismatch["field"] == expected_field
                        and mismatch["semantic_field"]
                        == expected_field.removeprefix(
                            SEMANTIC_MATERIALIZATION_SEARCH_RESULT_FIELD_PREFIX
                        )
                        and mismatch["code"] == expected_code
                        for mismatch in diff["field_mismatches"]
                    ),
                    diff,
                )

    def test_semantic_materialization_diff_rejects_field_mismatch(self) -> None:
        run_dir = self.write_semantic_materialization_fixture(visual=False)
        payload = self.load_json(run_dir / "search_tasks.json")
        payload["tasks"][0]["query"] = "rewritten downstream query"
        self.write_json(run_dir / "search_tasks.json", payload)

        diff = build_semantic_materialization_diff(
            run_dir=run_dir,
            require_research_tasks=True,
            require_downstream=True,
        )

        self.assertFalse(diff["valid"])
        self.assertTrue(
            any(mismatch["field"] == "query" for mismatch in diff["field_mismatches"]),
            diff,
        )

    def test_semantic_materialization_diff_rejects_missing_extra_and_duplicate_tasks(self) -> None:
        run_dir = self.write_semantic_materialization_fixture(visual=False)
        research = self.load_json(run_dir / "research_tasks.json")
        research["tasks"].pop()
        self.write_json(run_dir / "research_tasks.json", research)
        search = self.load_json(run_dir / "search_tasks.json")
        search["tasks"].append({
            **search["tasks"][0],
            "task_id": "task_extra_999",
            "semantic_plan_task_id": "task_extra_999",
            "id": "task_extra_999",
        })
        search["tasks"].append(dict(search["tasks"][0]))
        self.write_json(run_dir / "search_tasks.json", search)

        diff = build_semantic_materialization_diff(
            run_dir=run_dir,
            require_research_tasks=True,
            require_downstream=True,
        )

        self.assertFalse(diff["valid"])
        self.assertIn("task_search_003", diff["missing_task_ids"])
        self.assertIn("task_extra_999", diff["extra_task_ids"])
        self.assertIn("task_search_001", diff["duplicate_semantic_task_ids"])

    def test_semantic_materialization_diff_rejects_dropped_visual_obligations_and_lineage_failures(self) -> None:
        run_dir = self.write_semantic_materialization_fixture()
        self.write_json(run_dir / "visual_tasks.json", {"tasks": []})
        assignments = self.read_jsonl(run_dir / "subagent_assignments.jsonl")
        assignments[0].pop("angle_id")
        self.write_jsonl(run_dir / "subagent_assignments.jsonl", assignments)

        diff = build_semantic_materialization_diff(
            run_dir=run_dir,
            require_research_tasks=True,
            require_downstream=True,
        )

        self.assertFalse(diff["valid"])
        self.assertEqual(
            diff["dropped_visual_obligations"],
            ["task_search_001", "task_search_002", "task_search_003"],
        )
        self.assertTrue(diff["lineage_failures"], diff)

    def test_semantic_materialization_diff_requires_observed_image_for_each_visual_obligation(self) -> None:
        run_dir = self.write_semantic_materialization_fixture()
        self.realign_semantic_materialization_fixture(run_dir)
        missing_task_id = "task_search_002"

        candidates = []
        for record in self.read_jsonl(run_dir / "visual_candidates.jsonl"):
            if record["semantic_plan_task_id"] == missing_task_id:
                record["candidate_status"] = "budget_pruned"
                record["policy_decision"] = "budget_pruned"
                record["rejection_reason"] = "budget_pruned"
            candidates.append(record)
        self.write_jsonl(run_dir / "visual_candidates.jsonl", candidates)

        fetches = []
        for record in self.read_jsonl(run_dir / "image_fetch_status.jsonl"):
            if record["semantic_plan_task_id"] == missing_task_id:
                record["fetch_status"] = "budget_pruned"
                record["policy_decision"] = "budget_pruned"
                record["failure_code"] = "budget_pruned"
                record["local_artifact_path"] = None
                record["evidence_image_id"] = None
            fetches.append(record)
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", fetches)

        observations = [
            record
            for record in self.read_jsonl(run_dir / "visual_observations.jsonl")
            if record["semantic_plan_task_id"] != missing_task_id
        ]
        self.write_jsonl(run_dir / "visual_observations.jsonl", observations)

        evidence = self.load_json(run_dir / "evidence.json")
        evidence["images"] = [
            record
            for record in evidence["images"]
            if record["semantic_plan_task_id"] != missing_task_id
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        diff = build_semantic_materialization_diff(
            run_dir=run_dir,
            require_research_tasks=True,
            require_downstream=True,
        )

        self.assertFalse(diff["valid"])
        self.assertIn(missing_task_id, diff["missing_task_ids"])
        failed_artifacts = {
            check["artifact"]
            for check in diff["artifact_checks"]
            if check.get("valid") is not True
        }
        self.assertIn("visual_observations", failed_artifacts)
        self.assertIn("evidence.images", failed_artifacts)
        self.assertNotIn(missing_task_id, diff["dropped_visual_obligations"])

    def test_semantic_materialization_diff_rejects_missing_and_mismatched_lineage_hashes(self) -> None:
        run_dir = self.write_semantic_materialization_fixture(visual=False)
        search = self.load_json(run_dir / "search_tasks.json")
        search["tasks"][0].pop("semantic_plan_hash")
        search["tasks"][1]["semantic_plan_hash"] = "0" * 64
        self.write_json(run_dir / "search_tasks.json", search)
        assignments = self.read_jsonl(run_dir / "subagent_assignments.jsonl")
        assignments[0].pop("approved_delta_id")
        assignments[1]["approved_delta_id"] = "wrong_delta"
        self.write_jsonl(run_dir / "subagent_assignments.jsonl", assignments)

        diff = build_semantic_materialization_diff(
            run_dir=run_dir,
            require_research_tasks=True,
            require_downstream=True,
        )

        self.assertFalse(diff["valid"])
        codes = {failure["code"] for failure in diff["lineage_failures"]}
        self.assertIn("semantic_plan_hash_missing", codes)
        self.assertIn("semantic_plan_hash_mismatch", codes)
        self.assertIn("approved_delta_id_missing", codes)
        self.assertIn("approved_delta_id_mismatch", codes)

    def test_semantic_materialization_diff_requires_approved_delta_before_fanout(self) -> None:
        run_dir = self.write_semantic_materialization_fixture(visual=False)
        search = self.load_json(run_dir / "search_tasks.json")
        search["tasks"].append({
            **search["tasks"][0],
            "task_id": "task_extra_999",
            "semantic_plan_task_id": "task_extra_999",
            "id": "task_extra_999",
        })
        self.write_json(run_dir / "search_tasks.json", search)

        invalid = build_semantic_materialization_diff(
            run_dir=run_dir,
            require_research_tasks=True,
            require_downstream=True,
        )
        self.assertFalse(invalid["valid"])

        self.write_json(
            run_dir / "semantic_plan_delta.json",
            {
                "delta_applied": True,
                "approved_delta_id": "base_plan",
                "reviewer_approved": True,
                "created_before_fanout": True,
                "repair_categories": [],
            },
        )
        approved = build_semantic_materialization_diff(
            run_dir=run_dir,
            require_research_tasks=True,
            require_downstream=True,
        )
        self.assertTrue(approved["valid"], approved)

    def test_approved_delta_does_not_bypass_missing_required_downstream_artifacts(self) -> None:
        run_dir = self.write_semantic_materialization_fixture(visual=False)
        (run_dir / "search_results.jsonl").unlink()
        self.write_json(
            run_dir / "semantic_plan_delta.json",
            {
                "delta_applied": True,
                "approved_delta_id": "base_plan",
                "reviewer_approved": True,
                "created_before_fanout": True,
                "repair_categories": [],
            },
        )

        diff = build_semantic_materialization_diff(
            run_dir=run_dir,
            require_research_tasks=True,
            require_downstream=True,
        )

        self.assertFalse(diff["valid"])
        self.assertIn("search_results", diff["missing_required_artifacts"])
        self.assertTrue(
            any(
                failure["code"] == "semantic_materialization_missing_required_artifacts"
                for failure in diff["failures"]
            ),
            diff,
        )

    def test_prepare_replaces_stub_materialization_diff_for_accepted_semantic_plan(self) -> None:
        result, _adapter_request = self.prepare_with_codex_adapter(
            "Research public water system resilience with official records"
        )
        run_dir = Path(result["run_dir"])
        diff = self.load_json(run_dir / "semantic_materialization_diff.json")

        self.assertNotEqual(diff.get("status"), "stub_only")
        self.assertTrue(diff["full_materialization_validation_implemented"])
        self.assertTrue(diff["valid"], diff)
        self.assertEqual(diff["missing_task_ids"], [])
        self.assertEqual(diff["extra_task_ids"], [])

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
            "visual_example",
            "visual_observation",
            "official_source",
            "recent_change",
            "comparative_analysis",
            "counter_evidence",
        )
        angle_focus = {
            "primary_source": ("baseline primary evidence", "baseline source dossier"),
            "visual_example": ("representative image examples", "visual example set"),
            "official_source": ("official regulatory records", "official records matrix"),
            "visual_observation": ("image chart figure inspection", "visual evidence table"),
            "recent_change": ("recent 2026 timeline changes", "recent-change timeline"),
            "comparative_analysis": ("jurisdiction comparison implications", "comparison matrix"),
            "counter_evidence": ("counter evidence caveats", "counter-evidence register"),
        }
        generic_angle_tokens = {
            "and",
            "answer",
            "authoritative",
            "compare",
            "directly",
            "does",
            "discovery",
            "do",
            "evidence",
            "find",
            "for",
            "from",
            "how",
            "official",
            "primary",
            "public",
            "question",
            "research",
            "source",
            "sources",
            "support",
            "supports",
            "supporting",
            "that",
            "the",
            "what",
            "with",
            "which",
        }
        question_anchor_tokens = [
            token.strip(".,:;!?()[]{}\"'").lower()
            for token in question.split()
            if len(token.strip(".,:;!?()[]{}\"'")) > 2
            and token.strip(".,:;!?()[]{}\"'").lower() not in generic_angle_tokens
        ] or ["requested", "subject"]

        def angle_anchor_phrase(angle_index: int) -> str:
            first_index = ((angle_index - 1) * 2) % len(question_anchor_tokens)
            second_index = (first_index + 1) % len(question_anchor_tokens)
            return " ".join(
                dict.fromkeys(
                    [
                        question_anchor_tokens[first_index],
                        question_anchor_tokens[second_index],
                    ]
                )
            )

        for angle_index in range(1, angle_count + 1):
            angle_id = f"angle_{angle_index:03d}"
            route = "visual_required" if angle_index in visual_angle_indexes else "text_only"
            visual_targets = [f"{question} image evidence"] if route != "text_only" else []
            evidence_need = evidence_needs[(angle_index - 1) % len(evidence_needs)]
            focus_text, artifact_name = angle_focus[evidence_need]
            anchor_phrase = angle_anchor_phrase(angle_index)
            expected_artifacts = [artifact_name, f"adapter evidence notes {angle_index}"]
            angles.append(
                {
                    "angle_id": angle_id,
                    "title": f"{focus_text.title()} for {anchor_phrase.title()}",
                    "research_question": (
                        f"How does the {artifact_name} resolve {anchor_phrase}?"
                    ),
                    "why_this_angle_matters": f"Angle {angle_index} covers a distinct part of {question}.",
                    "included_scope": [question],
                    "excluded_scope": ["Do not substitute a local template inventory."],
                    "route": route,
                    "evidence_need": evidence_need,
                    "expected_source_types": source_types,
                    "expected_visual_targets": visual_targets,
                    "expected_artifacts": expected_artifacts,
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
                expected_artifacts = [artifact_name, f"adapter evidence notes {angle_index}"]
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

    def oracle_adapter_response(
        self,
        request: dict,
        *,
        question_scope: str = "broad",
        requirement_types: tuple[str, ...] = ("subject",),
        **_adapter_kwargs: object,
    ) -> dict:
        question = request["original_question"]
        requirements = []
        for index, requirement_type in enumerate(requirement_types, start=1):
            prompt_text = {
                "subject": question,
                "source_quality": "official primary sources",
                "visual_modality": "images",
                "time_range": "2026",
                "geography": "Korea",
                "deliverable_shape": "table",
            }.get(requirement_type, requirement_type)
            start = question.find(prompt_text)
            requirements.append(
                {
                    "requirement_id": f"req_{index:03d}",
                    "prompt_span": {
                        "start": start if start >= 0 else None,
                        "end": start + len(prompt_text) if start >= 0 else None,
                    },
                    "prompt_text": prompt_text,
                    "requirement_text": prompt_text,
                    "requirement_type": requirement_type,
                    "expected_entities": [question],
                    "expected_modalities": (
                        ["visual"] if requirement_type == "visual_modality" else ["text"]
                    ),
                    "source_quality_constraints": (
                        ["official", "regulatory", "primary"]
                        if requirement_type == "source_quality"
                        else []
                    ),
                    "geography_constraints": (
                        ["Korea"] if requirement_type == "geography" else []
                    ),
                    "time_constraints": (
                        ["2026"] if requirement_type == "time_range" else []
                    ),
                    "output_shape_constraints": (
                        ["table"] if requirement_type == "deliverable_shape" else []
                    ),
                    "expected_coverage": "full",
                    "explicit": True,
                    "inferred": False,
                    "inferred_reason": None,
                    "non_negotiable": True,
                }
            )
        expected_modalities = ["text"]
        if "visual_modality" in requirement_types:
            expected_modalities.append("visual")
        return {
            "schema_version": "codex-deepresearch.semantic-planner.v0",
            "artifact_type": "semantic_oracle_raw_response",
            "oracle_adapter": "codex_native_semantic_expectation_oracle",
            "prompt_version": "p3-sp3-oracle-v1",
            "semantic_release_eligible": False,
            "model_or_surface": "codex-oracle-test-double",
            "provenance": {
                "adapter_invocation_id": "oracle-test-invocation-001",
                "adapter_invocation_kind": CODEX_SEMANTIC_ADAPTER_INVOCATION_KIND,
                "raw_response_id": "oracle-test-response-001",
                "raw_request_hash": request["raw_request_hash"],
                "model_or_surface": "codex-oracle-test-double",
                "child_session_id": "codex-child-oracle-test-001",
            },
            "expectation_oracle": {
                "oracle_requirement_map": requirements,
                "question_scope": question_scope,
                "bounded_task_range": {
                    "min": 20 if question_scope == "broad" else 6,
                    "max": 40 if question_scope == "broad" else 12,
                    "depth_preset": request["depth_preset"],
                },
                "expected_entities": [
                    {"name": question, "type": "question_subject", "evidence": question}
                ],
                "expected_constraints": requirements,
                "expected_modalities": expected_modalities,
                "required_angles": [
                    {"angle_requirement": "source baseline", "subject": question},
                    {"angle_requirement": "counter-evidence and caveats", "subject": question},
                ],
                "forbidden_angles": [
                    "Codex runner architecture",
                    "semantic planner implementation",
                    "generic report wrapper",
                ],
                "forbidden_internal_implementation_terms": [
                    "technical_api",
                    "product_market",
                    "visual_style",
                    "policy_risk",
                    "heuristic_template_planner",
                    "local deterministic template",
                ],
                "expected_report_shape": (
                    ["table"] if "deliverable_shape" in requirement_types else ["report"]
                ),
                "language": "en",
            },
        }

    def reviewer_adapter_response(self, request: dict) -> dict:
        return {
            "schema_version": "codex-deepresearch.semantic-planner.v0",
            "artifact_type": "semantic_reviewer_raw_response",
            "reviewer_adapter": "codex_native_semantic_fit_reviewer",
            "prompt_version": "p3-sp3-reviewer-v1",
            "semantic_release_eligible": False,
            "model_or_surface": "codex-reviewer-test-double",
            "provenance": {
                "adapter_invocation_id": "reviewer-test-invocation-001",
                "adapter_invocation_kind": CODEX_SEMANTIC_ADAPTER_INVOCATION_KIND,
                "raw_response_id": "reviewer-test-response-001",
                "raw_request_hash": request["raw_request_hash"],
                "model_or_surface": "codex-reviewer-test-double",
                "child_session_id": "codex-child-reviewer-test-001",
            },
            "semantic_plan_review": {
                "semantic_fit_score": 9.6,
                "score_dimensions": {
                    "intent_preservation": 9.6,
                    "required_entities_constraints": 9.5,
                    "angle_relevance_diversity": 9.4,
                    "modality_visual_routing": 9.5,
                    "forbidden_drift_avoidance": 9.7,
                    "executable_bounded_tasks": 9.5,
                },
                "blockers": [],
                "warnings": [],
                "substitute_implementation_check": {"passed": True},
                "non_negotiable_coverage_complete": True,
                "verdict": "pass",
            },
        }

    def prepare_with_codex_adapter(
        self,
        question: str,
        *,
        stdout_format: str = "json",
        response_mutator: object | None = None,
        oracle_configured: bool = True,
        reviewer_configured: bool = True,
        command_env_configured: bool = True,
        default_codex_available: bool = False,
        route: str | None = None,
        **adapter_kwargs: object,
    ) -> tuple[dict, dict]:
        captured = {"commands": []}

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
            captured["commands"].append(list(command))
            self.assertFalse(check)
            self.assertTrue(capture_output)
            self.assertTrue(text)
            self.assertGreater(timeout, 0)
            request = json.loads(input)
            artifact_type = request.get("artifact_type")
            if artifact_type == "semantic_oracle_raw_request":
                response = self.oracle_adapter_response(request, **adapter_kwargs)
            elif artifact_type == "semantic_reviewer_raw_request":
                response = self.reviewer_adapter_response(request)
            else:
                captured["request"] = dict(request)
                response = self.codex_adapter_response(request, **adapter_kwargs)
            if artifact_type == "semantic_planner_raw_request" and callable(response_mutator):
                response = response_mutator(response)
            if stdout_format == "jsonl":
                role = str(artifact_type or "semantic")
                stdout = "\n".join(
                    [
                        json.dumps(
                            {
                                "id": f"{role}-event-001",
                                "type": "session.started",
                                "session_id": f"{role}-session-jsonl-001",
                            },
                            sort_keys=True,
                        ),
                        json.dumps(
                            {
                                "id": f"{role}-event-002",
                                "type": "message",
                                "session_id": f"{role}-session-jsonl-001",
                                "response": response,
                            },
                            sort_keys=True,
                        ),
                    ]
                )
            elif stdout_format in {"jsonl_item_text", "jsonl_item_text_reused_item_id"}:
                role = str(artifact_type or "semantic")
                item_id = (
                    "item_0"
                    if stdout_format == "jsonl_item_text_reused_item_id"
                    else f"{role}-item-001"
                )
                stdout = "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "thread.started",
                                "thread_id": f"{role}-thread-jsonl-001",
                            },
                            sort_keys=True,
                        ),
                        json.dumps({"type": "turn.started"}, sort_keys=True),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "id": item_id,
                                    "type": "agent_message",
                                    "text": json.dumps(response, sort_keys=True),
                                },
                            },
                            sort_keys=True,
                        ),
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {"input_tokens": 1, "output_tokens": 1},
                            },
                            sort_keys=True,
                        ),
                    ]
                )
            else:
                stdout = json.dumps(response, sort_keys=True)
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        env = {}
        if command_env_configured:
            env[CODEX_SEMANTIC_PLANNER_COMMAND_ENV] = "codex exec --json"
        if command_env_configured and oracle_configured:
            env[CODEX_SEMANTIC_ORACLE_COMMAND_ENV] = "codex exec --json"
        if command_env_configured and reviewer_configured:
            env[CODEX_SEMANTIC_REVIEWER_COMMAND_ENV] = "codex exec --json"
        if default_codex_available:
            env[CODEX_SEMANTIC_ENABLE_DEFAULT_ADAPTER_ENV] = "1"
        with mock.patch(
            "deepresearch.semantic_planner.subprocess.run",
            side_effect=fake_run,
        ), mock.patch.dict(
            "os.environ",
            env,
            clear=not command_env_configured,
        ), mock.patch(
            "deepresearch.semantic_planner.shutil.which",
            return_value="codex" if default_codex_available else None,
        ):
            result = prepare_run(
                question=question,
                runs_dir=self.temp_runs_dir(),
                route=route,
            )
        request = dict(captured["request"])
        request["_commands"] = list(captured["commands"])
        return result, request

    def text_only_oauth_candidate(self, *, question: str) -> dict:
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "c" * 64,
        }
        response = self.codex_adapter_response(
            request,
            question_scope="broad",
            angle_count=5,
            tasks_per_angle=4,
            requirement_types=("subject", "source_quality"),
            visual_angle_indexes=(),
        )
        candidate = json.loads(json.dumps(response["candidate_plan"]))
        subject = "OAuth device-flow provider documentation"
        angle_texts = [
            (
                "OAuth Device Flow Authorization Endpoint Documentation",
                "Which provider documentation defines OAuth device flow authorization endpoint behavior?",
            ),
            (
                "Implementation Error Handling Guidance",
                "Which OAuth provider guidance describes device flow polling errors and recovery?",
            ),
            (
                "Token Response Timing Requirements",
                "Which OAuth device flow documentation explains provider token timing and expiration?",
            ),
            (
                "Client Verification and User Code Steps",
                "Which provider implementation guidance covers device flow user code verification?",
            ),
            (
                "Security Caveats and Rate Limit Policies",
                "Which OAuth provider documentation states device flow security caveats and rate limits?",
            ),
        ]
        for index, angle in enumerate(candidate["angles"], start=1):
            angle["route"] = "text_only"
            angle["expected_visual_targets"] = []
            angle["evidence_need"] = "primary_source"
            angle["title"], angle["research_question"] = angle_texts[index - 1]
            angle["why_this_angle_matters"] = (
                "This preserves the official documentation comparison."
            )
            angle["included_scope"] = [subject]
            angle["excluded_scope"] = ["Out-of-scope provider implementation details."]
            angle["expected_source_types"] = ["official provider documentation"]
            angle["expected_artifacts"] = ["source-backed guidance notes"]
            angle["search_queries"] = [f"{subject} official documentation"]
            angle["success_criteria"] = [
                "Use official provider documentation as evidence."
            ]
            angle["report_section"] = f"Official Source Angle {index}"
            angle["risk_or_contradiction_checks"] = [
                "Check provider guidance differences."
            ]
        for index, task in enumerate(candidate["bounded_tasks"], start=1):
            task["route"] = "text_only"
            task["expected_visual_targets"] = []
            task["max_images"] = 0
            task["query"] = f"{subject} official provider documentation task {index}"
            task["expected_source_types"] = ["official provider documentation"]
            task["expected_artifacts"] = ["source-backed guidance notes"]
            task["success_criteria"] = [
                "Use official provider documentation as evidence."
            ]
            task["done_condition"] = "Stop when source-backed guidance notes are ready."
        return candidate

    def prepare_fixture(self, fixture: dict) -> Path:
        fallback_plan = heuristic_template_planner(question=fixture["question"])
        result = prepare_run(
            question=fixture["question"],
            runs_dir=self.temp_runs_dir(),
            angles=[angle.title for angle in fallback_plan.angles],
            _allow_release_ineligible_materialization_for_tests=True,
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

    def assert_invalid_adapter_response_blocked(
        self,
        result: dict,
        *,
        expected_failure_codes: set[str],
    ) -> None:
        run_dir = Path(result["run_dir"])
        evidence = self.load_json(run_dir / "evidence.json")
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )
        semantic_plan_artifact = self.load_json(run_dir / "semantic_plan.json")
        semantic_plan = semantic_plan_artifact["semantic_plan"]

        self.assertEqual(result["status"], "blocked_semantic_planner_unavailable")
        self.assertEqual(result["semantic_planning_status"], "blocked_semantic_planner_unavailable")
        self.assertEqual(result["planner_mode"], PLANNER_MODE_BLOCKED)
        self.assertFalse(result["semantic_release_eligible"])
        self.assertEqual(evidence["semantic_angles"], [])
        self.assertEqual(evidence["search_tasks"], [])
        self.assertFalse((run_dir / "search_tasks.json").exists())
        self.assertFalse((run_dir / "visual_tasks.json").exists())
        self.assertFalse((run_dir / "research_tasks.json").exists())
        self.assertEqual(semantic_plan["planner_mode"], PLANNER_MODE_BLOCKED)
        self.assertEqual(semantic_plan["angles"], [])
        self.assertEqual(semantic_plan["bounded_tasks"], [])

        self.assertEqual(raw_response["failure_category"], "adapter_invalid_response")
        candidate_validation = raw_response["adapter_response"]["candidate_validation"]
        self.assertFalse(candidate_validation["ok"], candidate_validation)
        failure_codes = {
            failure["code"] for failure in candidate_validation["failures"]
        }
        self.assertTrue(
            expected_failure_codes.issubset(failure_codes),
            failure_codes,
        )
        diagnostic_codes = set(
            raw_response["diagnostics"]["candidate_validation_failure_codes"]
        )
        self.assertTrue(expected_failure_codes.issubset(diagnostic_codes))

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
        self.assertTrue(review["blockers"])

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

    def semantic_internal_leakage_review_plan(
        self,
        *,
        query: str = "Compare official source evidence for the requested report.",
        expected_artifacts: list[str] | None = None,
        success_criteria: list[str] | None = None,
        done_condition: str = "Stop when source-backed findings are ready for the report.",
    ) -> SemanticPlan:
        artifacts = expected_artifacts or ["source-backed comparison notes"]
        criteria = success_criteria or ["Findings must cite source metadata."]
        angle = SemanticAngle(
            angle_id="angle_001",
            title="Source-backed comparison",
            research_question=query,
            question_context="Compare official source evidence for the requested report.",
            route="text_only",
            evidence_need="comparative_analysis",
            expected_artifacts=list(artifacts),
            success_criteria=list(criteria),
            report_section="Comparison",
            why_this_angle_matters="This angle preserves the requested deliverable shape.",
            included_scope=["Official source evidence"],
            expected_source_types=["official sources"],
            search_queries=[query],
        )
        return SemanticPlan(
            schema_version="codex-deepresearch.semantic-planner.v0",
            question_class="product_market",
            broad_question=False,
            source="codex_semantic",
            expected_evidence_needs=["comparative_analysis"],
            angles=[angle],
            intent_summary="Compare official source evidence for the requested report.",
            bounded_tasks=[
                {
                    "task_id": "task_semantic_001",
                    "angle_id": "angle_001",
                    "query": query,
                    "route": "text_only",
                    "source_policy": {"decision": "allowed"},
                    "expected_source_types": ["official sources"],
                    "expected_visual_targets": [],
                    "expected_artifacts": list(artifacts),
                    "success_criteria": list(criteria),
                    "max_sources": 3,
                    "max_images": 0,
                    "done_condition": done_condition,
                }
            ],
            planner_mode=PLANNER_MODE_CODEX_SEMANTIC,
        )

    def semantic_internal_leakage_oracle(self) -> dict:
        return {
            "forbidden_angles": [],
            "forbidden_internal_implementation_terms": [
                "planner",
                "subagent",
                "oracle",
                "budget",
                "run_id",
                "adapter",
                "schema",
                "requirement_id",
                "local deterministic template",
            ],
        }

    def test_semantic_substitute_check_allows_table_report_schema_deliverables(self) -> None:
        oracle = self.semantic_internal_leakage_oracle()
        plan = self.semantic_internal_leakage_review_plan(
            expected_artifacts=[
                "comparison_table_schema",
                "table schema",
                "report schema",
            ],
            success_criteria=[
                "Produce a comparison table schema with report columns.",
                "Describe the report schema as a deliverable structure.",
            ],
        )

        substitute = _semantic_substitute_implementation_check(plan=plan, oracle=oracle)

        self.assertFalse(_has_forbidden_internal_leakage(plan=plan, oracle=oracle))
        self.assertTrue(substitute["passed"], substitute)
        self.assertEqual(
            substitute["forbidden_internal_implementation_terms_found"],
            [],
        )

    def test_semantic_substitute_check_blocks_internal_implementation_leakage(self) -> None:
        oracle = self.semantic_internal_leakage_oracle()
        cases = (
            (
                "semantic planner implementation",
                "Document semantic planner implementation work before fan-out.",
                {"planner"},
            ),
            (
                "local deterministic template",
                "Use local deterministic template output as the research plan.",
                {"local deterministic template"},
            ),
            (
                "run_id adapter schema contract",
                "Implement run_id propagation through the adapter schema contract.",
                {"run_id", "adapter", "schema"},
            ),
            (
                "oracle subagent implementation",
                "Describe oracle and subagent implementation work.",
                {"oracle", "subagent"},
            ),
        )

        for label, query, expected_terms in cases:
            with self.subTest(label=label):
                plan = self.semantic_internal_leakage_review_plan(
                    query=query,
                    success_criteria=[query],
                )
                substitute = _semantic_substitute_implementation_check(
                    plan=plan,
                    oracle=oracle,
                )

                self.assertTrue(_has_forbidden_internal_leakage(plan=plan, oracle=oracle))
                self.assertFalse(substitute["passed"], substitute)
                self.assertTrue(
                    expected_terms.issubset(
                        set(substitute["forbidden_internal_implementation_terms_found"])
                    ),
                    substitute,
                )

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

    def test_validation_failure_demotes_persisted_semantic_release_eligibility(self) -> None:
        run_dir = self.temp_runs_dir() / "planner-validation-demotion"
        evidence = {
            "run_id": run_dir.name,
            "question": "Compare market, policy, implementation, release, and UI visual evidence.",
            "semantic_planner": {
                "planner_mode": PLANNER_MODE_CODEX_SEMANTIC,
                "semantic_release_eligible": True,
                "question_class": "product_market",
                "broad_question": True,
                "expected_evidence_needs": [
                    "primary_source",
                    "comparative_analysis",
                    "pricing_or_limits",
                    "user_workflow",
                    "visual_example",
                    "visual_observation",
                ],
            },
            "semantic_angles": [
                {
                    "id": "angle_001",
                    "angle_id": "angle_001",
                    "route": "visual_required",
                    "evidence_need": "visual_example",
                }
            ],
        }
        self.write_json(run_dir / "evidence.json", evidence)
        self.write_json(
            run_dir / "semantic_plan.json",
            {
                "semantic_release_eligible": True,
                "semantic_plan": {"semantic_release_eligible": True},
            },
        )
        self.write_json(
            run_dir / "status.json",
            {
                "semantic_release_eligible": True,
                "semantic_planning": {
                    "semantic_release_eligible": True,
                    "validation_ok": True,
                    "failure_codes": [],
                },
            },
        )

        validation = write_semantic_planner_validation(
            run_dir=run_dir,
            evidence=evidence,
            tasks=[],
        )

        self.assertFalse(validation["ok"], validation)
        self.assertFalse(validation["semantic_release_eligible"])
        self.assertTrue(validation["declared_semantic_release_eligible"])
        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertIn("broad_question_angle_count_out_of_range", failure_codes)
        persisted_evidence = self.load_json(run_dir / "evidence.json")
        persisted_plan = self.load_json(run_dir / "semantic_plan.json")
        persisted_status = self.load_json(run_dir / "status.json")
        self.assertFalse(persisted_evidence["semantic_release_eligible"])
        self.assertFalse(persisted_evidence["semantic_planner"]["semantic_release_eligible"])
        self.assertFalse(persisted_plan["semantic_release_eligible"])
        self.assertFalse(persisted_plan["semantic_plan"]["semantic_release_eligible"])
        self.assertFalse(persisted_status["semantic_release_eligible"])
        self.assertFalse(persisted_status["semantic_planning"]["semantic_release_eligible"])
        self.assertFalse(persisted_status["semantic_planning"]["validation_ok"])

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
                self.assertFalse(validation["semantic_release_eligible"])
                self.assertTrue(validation["declared_semantic_release_eligible"])
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

    def test_distinct_angles_may_share_evidence_need_without_material_difference_failure(self) -> None:
        run_dir = self.temp_runs_dir()
        evidence = {
            "run_id": "semantic-shared-evidence-need",
            "question": "Compare public health poster guidance across official sources.",
            "semantic_planner": {
                "fixture_id": "shared-evidence-need",
                "question_class": "policy",
                "broad_question": True,
                "expected_evidence_needs": [
                    "official_source",
                    "official_source",
                    "implementation_detail",
                    "failure_pattern",
                    "synthesis",
                ],
            },
            "semantic_angles": [
                {
                    "angle_id": "angle_001",
                    "title": "Central agency source set",
                    "research_question": "Which central agency sources define the poster guidance?",
                    "route": "text_only",
                    "evidence_need": "official_source",
                    "expected_artifacts": ["central source table"],
                    "success_criteria": ["Cite central agency records."],
                    "report_section": "Central Sources",
                },
                {
                    "angle_id": "angle_002",
                    "title": "Local agency source set",
                    "research_question": "Which local public health sources adapt the poster guidance?",
                    "route": "text_only",
                    "evidence_need": "official_source",
                    "expected_artifacts": ["local source table"],
                    "success_criteria": ["Cite local agency records."],
                    "report_section": "Local Sources",
                },
                {
                    "angle_id": "angle_003",
                    "title": "Message structure",
                    "research_question": "How do source messages differ by step, duration, and audience?",
                    "route": "text_only",
                    "evidence_need": "implementation_detail",
                    "expected_artifacts": ["message comparison"],
                    "success_criteria": ["Extract structured differences."],
                    "report_section": "Message Structure",
                },
                {
                    "angle_id": "angle_004",
                    "title": "Contradiction checks",
                    "research_question": "Which guidance differences are genuine contradictions?",
                    "route": "text_only",
                    "evidence_need": "failure_pattern",
                    "expected_artifacts": ["contradiction log"],
                    "success_criteria": ["Separate contradictions from wording differences."],
                    "report_section": "Contradictions",
                },
                {
                    "angle_id": "angle_005",
                    "title": "Synthesis limits",
                    "research_question": "What caveats should constrain the final comparison?",
                    "route": "text_only",
                    "evidence_need": "synthesis",
                    "expected_artifacts": ["caveat list"],
                    "success_criteria": ["State limits and confidence."],
                    "report_section": "Limits",
                },
            ],
        }

        validation = semantic_planner_validation(run_dir=run_dir, evidence=evidence, tasks=[])

        codes = {failure["code"] for failure in validation["failures"]}
        self.assertNotIn("semantic_angle_material_difference_failed", codes)

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

    def test_explicit_angle_input_blocks_before_materialization_by_default(self) -> None:
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

        self.assertEqual(result["status"], "blocked_semantic_review_failed")
        self.assertEqual(evidence["semantic_planner"]["source"], "manual_angles")
        self.assertEqual(evidence["semantic_planner"]["planner_mode"], PLANNER_MODE_MANUAL_ANGLES)
        self.assertFalse(evidence["semantic_planner"]["semantic_release_eligible"])
        self.assertEqual(evidence["semantic_angles"], [])
        self.assertEqual(evidence["search_tasks"], [])
        self.assertFalse((run_dir / "search_tasks.json").exists())
        self.assertFalse((run_dir / "visual_tasks.json").exists())
        self.assertFalse((run_dir / "research_tasks.json").exists())
        validation = self.load_json(run_dir / "semantic_planner_validation.json")
        self.assert_release_ineligible_semantic_validation(
            validation,
            planner_mode=PLANNER_MODE_MANUAL_ANGLES,
        )

    def test_route_override_preserves_heuristic_fanout_and_overrides_routes(self) -> None:
        fallback_plan = heuristic_template_planner(question=SEMANTIC_FIXTURES[0]["question"])
        result = prepare_run(
            question=SEMANTIC_FIXTURES[0]["question"],
            runs_dir=self.temp_runs_dir(),
            angles=[angle.title for angle in fallback_plan.angles],
            route="text_only",
            _allow_release_ineligible_materialization_for_tests=True,
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
            visual_angle_indexes=(2, 3),
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

    def test_text_only_planner_request_and_prompt_include_no_visual_contract(self) -> None:
        question = "Compare OAuth device-flow implementation guidance across official provider documentation."
        raw_request = build_codex_semantic_raw_request(
            question=question,
            visual_preference="text_only",
            budget_cap={"max_images": 4, "max_sources": 5},
        )

        self.assertEqual(raw_request["visual_preference"], "text_only")
        self.assertEqual(raw_request["budget_cap"]["max_images"], 0)
        request_contract = json.dumps(
            raw_request["planner_instructions"],
            ensure_ascii=False,
        ).lower()
        prompt_contract = _semantic_adapter_prompt("semantic planner").lower()
        for contract_text in (request_contract, prompt_contract):
            self.assertIn("visual_preference is text_only", contract_text)
            self.assertIn("all angles", contract_text)
            self.assertIn("bounded_tasks", contract_text)
            self.assertIn("route=text_only", contract_text)
            self.assertIn("max_images=0", contract_text)
            self.assertIn("expected_visual_targets=[]", contract_text)
            self.assertIn("at least 2 meaningful non-generic", contract_text)
            for forbidden_visual_work in (
                "no image",
                "chart",
                "screenshot",
                "diagram",
            ):
                self.assertIn(forbidden_visual_work, contract_text)

    def test_text_only_visual_preference_rejects_visual_candidate_before_review(self) -> None:
        question = "Compare OAuth device-flow implementation guidance across official provider documentation."
        result, adapter_request = self.prepare_with_codex_adapter(
            question,
            route="text_only",
            requirement_types=("subject", "source_quality"),
            visual_angle_indexes=(2, 3),
        )

        self.assert_invalid_adapter_response_blocked(
            result,
            expected_failure_codes={"text_only_visual_preference_violation"},
        )
        self.assertEqual(adapter_request["visual_preference"], "text_only")
        run_dir = Path(result["run_dir"])
        self.assertFalse(
            (run_dir / "semantic_reviewer_raw" / "reviewer_request.json").exists()
        )
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )
        candidate_validation = raw_response["adapter_response"]["candidate_validation"]
        failure = next(
            failure
            for failure in candidate_validation["failures"]
            if failure["code"] == "text_only_visual_preference_violation"
        )
        self.assertGreater(failure["violation_count"], 0)
        self.assertTrue(
            any(
                violation.get("field") == "route"
                and violation.get("value") == "visual_required"
                for violation in failure["violations"]
            ),
            failure,
        )

    def test_text_only_validation_rejects_visual_work_terms_without_visual_route(self) -> None:
        question = "Compare OAuth device-flow implementation guidance across official provider documentation."
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "b" * 64,
        }
        response = self.codex_adapter_response(
            request,
            question_scope="broad",
            angle_count=5,
            tasks_per_angle=4,
            requirement_types=("subject", "source_quality"),
            visual_angle_indexes=(),
        )
        candidate = json.loads(json.dumps(response["candidate_plan"]))
        for index, angle in enumerate(candidate["angles"], start=1):
            angle["route"] = "text_only"
            angle["expected_visual_targets"] = []
            angle["evidence_need"] = "primary_source"
            angle["title"] = f"{question} official source focus {index}"
            angle["research_question"] = f"Which official source guidance addresses {question}?"
            angle["why_this_angle_matters"] = "This preserves the official documentation comparison."
            angle["expected_source_types"] = ["official provider documentation"]
            angle["expected_artifacts"] = ["source-backed guidance notes"]
            angle["search_queries"] = [f"{question} official documentation"]
            angle["success_criteria"] = ["Use official provider documentation as evidence."]
            angle["report_section"] = f"Official Source Angle {index}"
            angle["risk_or_contradiction_checks"] = ["Check provider guidance differences."]
        for index, task in enumerate(candidate["bounded_tasks"], start=1):
            task["route"] = "text_only"
            task["expected_visual_targets"] = []
            task["max_images"] = 0
            task["query"] = f"{question} official provider documentation task {index}"
            task["expected_source_types"] = ["official provider documentation"]
            task["expected_artifacts"] = ["source-backed guidance notes"]
            task["success_criteria"] = ["Use official provider documentation as evidence."]
            task["done_condition"] = "Stop when source-backed guidance notes are ready."

        candidate["angles"][0]["evidence_need"] = "visual_example"
        candidate["angles"][1]["evidence_need"] = "visual_observation"
        candidate["angles"][2]["evidence_need"] = "vlm_analysis"
        candidate["angles"][3]["title"] = "Screenshot artifact comparison"
        candidate["angles"][4]["search_queries"] = [
            "OAuth provider device flow diagram documentation"
        ]
        candidate["bounded_tasks"][0][
            "query"
        ] = "Compare OAuth provider screenshot evidence in device-flow docs"
        candidate["bounded_tasks"][1]["expected_artifacts"] = [
            "provider chart evidence register"
        ]
        candidate["bounded_tasks"][2]["success_criteria"] = [
            "Compare diagram evidence using official provider docs."
        ]
        candidate["bounded_tasks"][3][
            "done_condition"
        ] = "Stop after VLM analysis is summarized."

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
            visual_preference="text_only",
        )

        self.assertFalse(validation["ok"], validation)
        failure = next(
            failure
            for failure in validation["failures"]
            if failure["code"] == "text_only_visual_preference_violation"
        )
        self.assertFalse(
            any(
                violation.get("field")
                in {"route", "expected_visual_targets", "max_images"}
                for violation in failure["violations"]
            ),
            failure,
        )
        violation_fields = {
            (violation.get("record_type"), violation.get("field"))
            for violation in failure["violations"]
        }
        self.assertTrue(
            {
                ("angle", "evidence_need"),
                ("angle", "title"),
                ("angle", "search_queries"),
                ("task", "query"),
                ("task", "expected_artifacts"),
                ("task", "success_criteria"),
                ("task", "done_condition"),
            }.issubset(violation_fields),
            failure,
        )
        evidence_need_matches = {
            match
            for violation in failure["violations"]
            if violation.get("field") == "evidence_need"
            for match in violation.get("matches", [])
        }
        self.assertTrue(
            {"visual_example", "visual_observation", "vlm_analysis"}.issubset(
                evidence_need_matches
            ),
            failure,
        )

    def test_text_only_visual_work_terms_block_adapter_candidate_before_review(self) -> None:
        question = "Compare OAuth device-flow implementation guidance across official provider documentation."

        def hide_visual_work_in_text_fields(response: dict) -> dict:
            response = json.loads(json.dumps(response))
            candidate = response["candidate_plan"]
            for index, angle in enumerate(candidate["angles"], start=1):
                angle["route"] = "text_only"
                angle["expected_visual_targets"] = []
                angle["evidence_need"] = "primary_source"
                angle["title"] = f"{question} official source focus {index}"
                angle["research_question"] = f"Which official source guidance addresses {question}?"
                angle["why_this_angle_matters"] = "This preserves the official documentation comparison."
                angle["expected_source_types"] = ["official provider documentation"]
                angle["expected_artifacts"] = ["source-backed guidance notes"]
                angle["search_queries"] = [f"{question} official documentation"]
                angle["success_criteria"] = ["Use official provider documentation as evidence."]
                angle["report_section"] = f"Official Source Angle {index}"
                angle["risk_or_contradiction_checks"] = ["Check provider guidance differences."]
            for index, task in enumerate(candidate["bounded_tasks"], start=1):
                task["route"] = "text_only"
                task["expected_visual_targets"] = []
                task["max_images"] = 0
                task["query"] = f"{question} official provider documentation task {index}"
                task["expected_source_types"] = ["official provider documentation"]
                task["expected_artifacts"] = ["source-backed guidance notes"]
                task["success_criteria"] = ["Use official provider documentation as evidence."]
                task["done_condition"] = "Stop when source-backed guidance notes are ready."
            candidate["angles"][0]["evidence_need"] = "visual_example"
            candidate["angles"][0]["expected_artifacts"] = [
                "provider screenshot evidence register"
            ]
            candidate["bounded_tasks"][0][
                "query"
            ] = "Compare OAuth provider diagram evidence in device-flow docs"
            return response

        result, adapter_request = self.prepare_with_codex_adapter(
            question,
            route="text_only",
            requirement_types=("subject", "source_quality"),
            response_mutator=hide_visual_work_in_text_fields,
        )

        self.assert_invalid_adapter_response_blocked(
            result,
            expected_failure_codes={"text_only_visual_preference_violation"},
        )
        self.assertEqual(adapter_request["visual_preference"], "text_only")
        run_dir = Path(result["run_dir"])
        self.assertFalse(
            (run_dir / "semantic_reviewer_raw" / "reviewer_request.json").exists()
        )
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )
        failure = next(
            failure
            for failure in raw_response["adapter_response"]["candidate_validation"][
                "failures"
            ]
            if failure["code"] == "text_only_visual_preference_violation"
        )
        self.assertFalse(
            any(
                violation.get("field")
                in {"route", "expected_visual_targets", "max_images"}
                for violation in failure["violations"]
            ),
            failure,
        )
        self.assertTrue(
            any(
                violation.get("field") == "evidence_need"
                and "visual_example" in violation.get("matches", [])
                for violation in failure["violations"]
            ),
            failure,
        )
        self.assertTrue(
            any(
                violation.get("field") == "query"
                and "diagram" in violation.get("matches", [])
                for violation in failure["violations"]
            ),
            failure,
        )

    def test_visual_keyword_detection_is_token_aware_for_oauth_guidance(self) -> None:
        self.assertFalse(
            question_mentions_visual_evidence(
                "Compare OAuth device-flow implementation guidance across official provider documentation."
            )
        )
        self.assertFalse(question_mentions_visual_evidence("Review device-flow guidance."))
        explicit_visual_questions = (
            "Compare OAuth provider screenshots.",
            "Review the provider UI.",
            "Find image evidence in provider docs.",
            "Compare provider chart examples.",
            "Inspect provider flow diagrams.",
        )
        for question in explicit_visual_questions:
            with self.subTest(question=question):
                self.assertTrue(question_mentions_visual_evidence(question))

    def test_text_only_allows_visual_terms_in_excluded_scope(self) -> None:
        question = "Compare OAuth device-flow implementation guidance across official provider documentation."
        candidate = self.text_only_oauth_candidate(question=question)
        for angle in candidate["angles"]:
            angle["excluded_scope"] = [
                "Visual inspection of provider login pages.",
                "Screenshots, UI review, and page diagrams are out of scope.",
            ]

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
            visual_preference="text_only",
        )

        self.assertTrue(validation["ok"], validation)
        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertNotIn("text_only_visual_preference_violation", failure_codes)

    def test_text_only_suppresses_visual_missing_failures_for_visual_exclusion_text(self) -> None:
        question = (
            "Compare OAuth device-flow implementation guidance across official provider "
            "documentation, excluding visual inspection and screenshots."
        )
        candidate = self.text_only_oauth_candidate(question=question)
        for angle in candidate["angles"]:
            angle["excluded_scope"] = [
                "Visual inspection of provider login pages.",
                "Screenshots and UI review are excluded.",
            ]

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
            visual_preference="text_only",
        )

        self.assertTrue(validation["ok"], validation)
        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertFalse(
            {
                "visual_requirement_missing_visual_route",
                "visual_requirement_missing_targets",
                "visual_example_expected_evidence_missing",
                "visual_observation_expected_evidence_missing",
            }
            & failure_codes,
            validation,
        )
        self.assertNotIn("text_only_visual_preference_violation", failure_codes)

    def test_default_codex_semantic_adapter_commands_use_schemas_when_env_absent(self) -> None:
        question = "Research lunar habitat material tests using official images and source records"
        result, adapter_request = self.prepare_with_codex_adapter(
            question,
            command_env_configured=False,
            default_codex_available=True,
            requirement_types=("subject", "source_quality", "visual_modality"),
            visual_angle_indexes=(2, 3),
        )
        run_dir = Path(result["run_dir"])

        self.assertEqual(result["status"], "awaiting_search_results")
        self.assertTrue(result["semantic_release_eligible"])
        captured_commands = adapter_request["_commands"]
        self.assertEqual(len(captured_commands), 3)
        raw_artifacts = (
            run_dir / "semantic_planner_raw" / "planner_response.json",
            run_dir / "semantic_oracle_raw" / "oracle_response.json",
            run_dir / "semantic_reviewer_raw" / "reviewer_response.json",
        )
        expected_schema_files = ("planner.json", "oracle.json", "reviewer.json")
        for raw_artifact, schema_file, captured_command in zip(
            raw_artifacts,
            expected_schema_files,
            captured_commands,
        ):
            with self.subTest(raw_artifact=raw_artifact.name):
                raw_response = self.load_json(raw_artifact)
                redacted_command = raw_response["provenance"]["adapter_command"]
                self.assertEqual(redacted_command[:3], ["codex", "exec", "--json"])
                self.assertIn("--output-schema", redacted_command)
                self.assertIn(
                    f"semantic_adapter_schemas/{schema_file}",
                    redacted_command[redacted_command.index("--output-schema") + 1],
                )
                self.assertIn("--output-schema", captured_command)
                self.assertIn("Return only JSON matching the provided schema", captured_command[-1])

        planner_command = next(
            command
            for command in captured_commands
            if "planner.json" in command[command.index("--output-schema") + 1]
        )
        self.assertIn(
            "semantically, not by keyword or fixed template",
            planner_command[-1],
        )

        reviewer_response = self.load_json(
            run_dir / "semantic_reviewer_raw" / "reviewer_response.json"
        )
        self.assertTrue(
            reviewer_response["semantic_plan_review"]["non_negotiable_coverage_complete"]
        )

    def test_oracle_is_locked_before_planner_request_with_trace_hashes(self) -> None:
        question = "Research lunar habitat material tests using official images and source records"
        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            requirement_types=("subject", "source_quality", "visual_modality"),
            visual_angle_indexes=(2, 3),
        )
        run_dir = Path(result["run_dir"])
        oracle_request = self.load_json(
            run_dir / "semantic_oracle_raw" / "oracle_request.json"
        )
        planner_request = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_request.json"
        )
        oracle = self.load_json(run_dir / "semantic_expectation_oracle.json")
        trace = self.read_jsonl(run_dir / "run_trace.jsonl")
        events = [record["event_type"] for record in trace]

        self.assertLess(
            events.index("semantic_oracle_request_created"),
            events.index("semantic_planner_request_created"),
        )
        self.assertLess(
            events.index("semantic_oracle_locked"),
            events.index("semantic_planner_request_created"),
        )
        expected_semantic_indexes = {
            "semantic_oracle_request_created": 1,
            "semantic_oracle_locked": 2,
            "semantic_planner_request_created": 3,
            "semantic_plan_created": 4,
            "semantic_reviewer_request_created": 5,
            "semantic_review_completed": 6,
        }
        by_event = {record["event_type"]: record for record in trace}
        for event, expected_index in expected_semantic_indexes.items():
            with self.subTest(event=event):
                self.assertEqual(
                    by_event[event]["semantic_event_index"],
                    expected_index,
                )
                self.assertEqual(
                    by_event[event]["order_validation"]["semantic_event_index"],
                    expected_index,
                )
        self.assertLess(
            by_event["semantic_oracle_request_created"]["semantic_event_index"],
            by_event["semantic_planner_request_created"]["semantic_event_index"],
        )
        self.assertLess(
            by_event["semantic_oracle_locked"]["semantic_event_index"],
            by_event["semantic_planner_request_created"]["semantic_event_index"],
        )
        self.assertLess(
            self.parse_timestamp(by_event["semantic_oracle_request_created"]["timestamp"]),
            self.parse_timestamp(by_event["semantic_planner_request_created"]["timestamp"]),
        )
        self.assertLess(
            self.parse_timestamp(by_event["semantic_oracle_locked"]["timestamp"]),
            self.parse_timestamp(by_event["semantic_planner_request_created"]["timestamp"]),
        )
        semantic_timestamps = [
            self.parse_timestamp(by_event[event]["timestamp"])
            for event in expected_semantic_indexes
        ]
        self.assertEqual(semantic_timestamps, sorted(semantic_timestamps))
        self.assertEqual(len(set(semantic_timestamps)), len(semantic_timestamps))
        self.assertEqual(oracle_request["original_question"], question)
        self.assertEqual(planner_request["original_question"], question)
        oracle_request_text = json.dumps(oracle_request, ensure_ascii=False).lower()
        forbidden_reverse_fit = (
            "candidate_plan",
            "semantic_plan",
            "planner_output",
            "technical_api",
            "product_market",
            "visual_style",
            "implementation_architecture",
        )
        for token in forbidden_reverse_fit:
            self.assertNotIn(token, oracle_request_text)
        self.assertFalse(oracle["plan_visible_to_oracle"])
        self.assertFalse(oracle["used_production_planner_output"])
        self.assertFalse(oracle["used_hidden_template_class"])
        self.assertFalse(oracle["used_fixed_angle_inventory"])

        oracle_hash = hashlib.sha256(
            (run_dir / "semantic_expectation_oracle.json").read_bytes()
        ).hexdigest()
        planner_request_hash = hashlib.sha256(
            (run_dir / "semantic_planner_raw" / "planner_request.json").read_bytes()
        ).hexdigest()
        self.assertEqual(
            by_event["semantic_oracle_locked"]["artifact_hashes"][
                "semantic_expectation_oracle"
            ],
            oracle_hash,
        )
        self.assertEqual(
            by_event["semantic_planner_request_created"]["artifact_hashes"][
                "semantic_planner_raw_request"
            ],
            planner_request_hash,
        )

    def test_missing_independent_reviewer_blocks_before_handoff_materialization(self) -> None:
        question = "Research lunar habitat material tests using official images and source records"
        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            requirement_types=("subject", "source_quality", "visual_modality"),
            visual_angle_indexes=(2, 3),
            reviewer_configured=False,
        )
        run_dir = Path(result["run_dir"])
        review = self.load_json(run_dir / "semantic_plan_review.json")
        trace = self.read_jsonl(run_dir / "run_trace.jsonl")

        self.assertEqual(result["status"], "blocked_semantic_review_failed")
        self.assertFalse((run_dir / "search_tasks.json").exists())
        self.assertFalse((run_dir / "visual_tasks.json").exists())
        self.assertFalse((run_dir / "research_tasks.json").exists())
        blocker_codes = {blocker["code"] for blocker in review["blockers"]}
        self.assertIn("reviewer_adapter_unavailable", blocker_codes)
        self.assertIn("non_release_reviewer_fixture", blocker_codes)
        events = [record["event_type"] for record in trace]
        for event in (
            "semantic_oracle_request_created",
            "semantic_oracle_locked",
            "semantic_planner_request_created",
            "semantic_plan_created",
            "semantic_reviewer_request_created",
            "semantic_review_completed",
        ):
            self.assertIn(event, events)

    def test_fabricated_eligible_artifacts_fail_without_trace_ordering(self) -> None:
        question = "Research lunar habitat material tests using official images and source records"
        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            requirement_types=("subject", "source_quality", "visual_modality"),
            visual_angle_indexes=(2, 3),
        )
        run_dir = Path(result["run_dir"])
        (run_dir / "run_trace.jsonl").unlink()
        evidence = self.load_json(run_dir / "evidence.json")
        search_tasks = self.load_json(run_dir / "search_tasks.json")["tasks"]

        validation = semantic_planner_validation(
            run_dir=run_dir,
            evidence=evidence,
            tasks=search_tasks,
        )

        self.assertFalse(validation["ok"], validation)
        semantic_codes = {
            failure["code"]
            for failure in validation["semantic_status"]["failures"]
        }
        self.assertIn("semantic_ordering_trace_missing", semantic_codes)

    def test_fabricated_trace_order_fails_without_monotonic_semantic_event_index(self) -> None:
        question = "Research lunar habitat material tests using official images and source records"
        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            requirement_types=("subject", "source_quality", "visual_modality"),
            visual_angle_indexes=(2, 3),
        )
        run_dir = Path(result["run_dir"])
        trace_path = run_dir / "run_trace.jsonl"
        records = self.read_jsonl(trace_path)
        for record in records:
            if record.get("event_type") == "semantic_oracle_locked":
                record["semantic_event_index"] = 3
                record["order_validation"]["semantic_event_index"] = 3
                break
        trace_path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
        evidence = self.load_json(run_dir / "evidence.json")
        search_tasks = self.load_json(run_dir / "search_tasks.json")["tasks"]

        validation = semantic_planner_validation(
            run_dir=run_dir,
            evidence=evidence,
            tasks=search_tasks,
        )

        self.assertFalse(validation["ok"], validation)
        semantic_codes = {
            failure["code"]
            for failure in validation["semantic_status"]["failures"]
        }
        self.assertIn("semantic_ordering_event_index_invalid", semantic_codes)

    def test_fabricated_trace_order_fails_without_strict_semantic_timestamps(self) -> None:
        question = "Research lunar habitat material tests using official images and source records"
        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            requirement_types=("subject", "source_quality", "visual_modality"),
            visual_angle_indexes=(2, 3),
        )
        run_dir = Path(result["run_dir"])
        trace_path = run_dir / "run_trace.jsonl"
        records = self.read_jsonl(trace_path)
        shared_timestamp = records[0]["timestamp"]
        for record in records:
            if str(record.get("event_type", "")).startswith("semantic_"):
                record["timestamp"] = shared_timestamp
        trace_path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
        evidence = self.load_json(run_dir / "evidence.json")
        search_tasks = self.load_json(run_dir / "search_tasks.json")["tasks"]

        validation = semantic_planner_validation(
            run_dir=run_dir,
            evidence=evidence,
            tasks=search_tasks,
        )

        self.assertFalse(validation["ok"], validation)
        semantic_codes = {
            failure["code"]
            for failure in validation["semantic_status"]["failures"]
        }
        self.assertIn(
            "semantic_ordering_event_timestamps_not_strictly_increasing",
            semantic_codes,
        )

    def test_generated_raw_request_hashes_match_final_oracle_and_reviewer_files(self) -> None:
        question = "Research lunar habitat material tests using official images and source records"
        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            requirement_types=("subject", "source_quality", "visual_modality"),
            visual_angle_indexes=(2, 3),
        )
        run_dir = Path(result["run_dir"])
        oracle = self.load_json(run_dir / "semantic_expectation_oracle.json")
        review = self.load_json(run_dir / "semantic_plan_review.json")

        cases = (
            (
                "oracle",
                Path(oracle["raw_request_path"]),
                oracle["raw_request_content_hash"],
                oracle["raw_request_artifact_hash"],
            ),
            (
                "reviewer",
                Path(review["reviewer_raw_request_path"]),
                review["reviewer_raw_request_content_hash"],
                review["reviewer_raw_request_artifact_hash"],
            ),
        )
        for label, request_path, expected_content_hash, expected_artifact_hash in cases:
            with self.subTest(label=label):
                self.assertTrue(request_path.is_file())
                self.assertEqual(
                    hashlib.sha256(request_path.read_bytes()).hexdigest(),
                    expected_artifact_hash,
                )
                raw_request = self.load_json(request_path)
                self.assertEqual(raw_request["raw_request_content_hash"], expected_content_hash)
                self.assertEqual(raw_request["raw_request_hash"], expected_content_hash)
                content_payload = dict(raw_request)
                content_payload.pop("raw_request_content_hash", None)
                content_payload.pop("raw_request_hash", None)
                actual_content_hash = hashlib.sha256(
                    json.dumps(
                        content_payload,
                        sort_keys=True,
                        ensure_ascii=True,
                    ).encode("utf-8")
                ).hexdigest()
                self.assertEqual(actual_content_hash, expected_content_hash)

    def test_fabricated_eligible_artifacts_fail_without_raw_artifacts_or_codex_identity(self) -> None:
        question = "Research lunar habitat material tests using official images and source records"
        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            requirement_types=("subject", "source_quality", "visual_modality"),
            visual_angle_indexes=(2, 3),
        )
        run_dir = Path(result["run_dir"])
        fake_hash = "0" * 64

        def strip_release_identity(provenance: dict) -> dict:
            for field in (
                "child_session_id",
                "session_id",
                "raw_response_id",
                "codex_event_id",
                "response_id",
            ):
                provenance.pop(field, None)
            provenance["session_id_unavailable_reason"] = "fabricated unavailable reason"
            return provenance

        oracle_path = run_dir / "semantic_expectation_oracle.json"
        oracle = self.load_json(oracle_path)
        oracle_provenance = strip_release_identity(dict(oracle["oracle_provenance"]))
        oracle_provenance.update(
            {
                "raw_request_path": "missing/oracle_request.json",
                "raw_response_path": "missing/oracle_response.json",
                "raw_request_hash": fake_hash,
                "raw_request_artifact_hash": fake_hash,
                "raw_response_hash": fake_hash,
                "raw_response_artifact_hash": fake_hash,
            }
        )
        oracle["oracle_provenance"] = oracle_provenance
        oracle["provenance"] = oracle_provenance
        oracle["raw_request_path"] = "missing/oracle_request.json"
        oracle["raw_response_path"] = "missing/oracle_response.json"
        oracle["raw_request_hash"] = fake_hash
        oracle["raw_request_artifact_hash"] = fake_hash
        oracle["raw_response_hash"] = fake_hash
        oracle["raw_response_artifact_hash"] = fake_hash
        self.write_json(oracle_path, oracle)

        plan_path = run_dir / "semantic_plan.json"
        plan = self.load_json(plan_path)
        plan_provenance = strip_release_identity(dict(plan["planner_provenance"]))
        plan["planner_provenance"] = plan_provenance
        if isinstance(plan.get("semantic_plan"), dict):
            plan["semantic_plan"]["planner_provenance"] = plan_provenance
        plan["raw_request_path"] = "missing/planner_request.json"
        plan["raw_response_path"] = "missing/planner_response.json"
        plan["raw_request_hash"] = fake_hash
        plan["raw_response_hash"] = fake_hash
        self.write_json(plan_path, plan)

        review_path = run_dir / "semantic_plan_review.json"
        review = self.load_json(review_path)
        reviewer_provenance = strip_release_identity(dict(review["reviewer_provenance"]))
        reviewer_provenance.update(
            {
                "raw_request_path": "missing/reviewer_request.json",
                "raw_response_path": "missing/reviewer_response.json",
                "raw_request_hash": fake_hash,
                "raw_request_artifact_hash": fake_hash,
                "raw_response_hash": fake_hash,
                "raw_response_artifact_hash": fake_hash,
            }
        )
        review["semantic_fit_score"] = 9.2
        review["blockers"] = []
        review["substitute_implementation_check"] = {"passed": True, "checked": True}
        review["verdict"] = "pass"
        review["final_verdict"] = "pass"
        review["non_negotiable_coverage_complete"] = True
        review["reviewer_independence"] = {"independent": True, "status": "passed"}
        review["reviewer_provenance"] = reviewer_provenance
        review["provenance"] = reviewer_provenance
        review["reviewer_raw_request_path"] = "missing/reviewer_request.json"
        review["reviewer_raw_response_path"] = "missing/reviewer_response.json"
        review["reviewer_raw_request_hash"] = fake_hash
        review["reviewer_raw_request_artifact_hash"] = fake_hash
        review["reviewer_raw_response_hash"] = fake_hash
        review["reviewer_raw_response_artifact_hash"] = fake_hash
        self.write_json(review_path, review)

        trace_path = run_dir / "run_trace.jsonl"
        records = self.read_jsonl(trace_path)
        for record in records:
            if str(record.get("event_type", "")).startswith("semantic_"):
                paths = record.get("semantic_artifact_paths")
                hashes = record.get("artifact_hashes")
                if isinstance(paths, dict) and isinstance(hashes, dict):
                    for key in list(hashes):
                        paths[key] = f"missing/{key}.json"
                        hashes[key] = fake_hash
        trace_path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )

        evidence = self.load_json(run_dir / "evidence.json")
        search_tasks = self.load_json(run_dir / "search_tasks.json")["tasks"]
        validation = semantic_planner_validation(
            run_dir=run_dir,
            evidence=evidence,
            tasks=search_tasks,
        )

        self.assertFalse(validation["ok"], validation)
        semantic_codes = {
            failure["code"]
            for failure in validation["semantic_status"]["failures"]
        }
        self.assertIn("semantic_planner_provenance_incomplete", semantic_codes)
        self.assertIn("semantic_oracle_provenance_incomplete", semantic_codes)
        self.assertIn("reviewer_provenance_incomplete", semantic_codes)
        self.assertIn("planner_raw_request_artifact_missing", semantic_codes)
        self.assertIn("oracle_raw_request_artifact_missing", semantic_codes)
        self.assertIn("reviewer_raw_request_artifact_missing", semantic_codes)
        self.assertIn("semantic_ordering_artifact_missing", semantic_codes)

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
        self.assertTrue(semantic_plan["semantic_release_eligible"])
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
            "max_results",
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
            "semantic_planner_raw_request-event-002",
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
        persisted_validation = self.load_json(run_dir / "semantic_planner_validation.json")
        self.assertTrue(persisted_validation["ok"], persisted_validation)

    def test_codex_semantic_accepts_current_codex_jsonl_item_text_shape(self) -> None:
        question = "Research coastal microgrid outage recovery using official source records"
        result, adapter_request = self.prepare_with_codex_adapter(
            question,
            stdout_format="jsonl_item_text",
            requirement_types=("subject", "source_quality"),
        )
        run_dir = Path(result["run_dir"])
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")["semantic_plan"]
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )
        oracle_response = self.load_json(
            run_dir / "semantic_oracle_raw" / "oracle_response.json"
        )
        reviewer_response = self.load_json(
            run_dir / "semantic_reviewer_raw" / "reviewer_response.json"
        )
        oracle = self.load_json(run_dir / "semantic_expectation_oracle.json")
        review = self.load_json(run_dir / "semantic_plan_review.json")

        self.assertEqual(result["planner_mode"], PLANNER_MODE_CODEX_SEMANTIC)
        self.assertTrue(result["semantic_release_eligible"])
        self.assertEqual(semantic_plan["planner_mode"], PLANNER_MODE_CODEX_SEMANTIC)
        self.assertIn("candidate_plan", raw_response)
        self.assertIn("expectation_oracle", oracle_response)
        self.assertIn("semantic_plan_review", reviewer_response)
        self.assertEqual(
            raw_response["provenance"]["raw_request_hash"],
            adapter_request["adapter_request_hash"],
        )
        cases = (
            (
                "planner",
                raw_response["provenance"],
                "semantic_planner_raw_request",
            ),
            ("oracle", oracle_response["provenance"], "semantic_oracle_raw_request"),
            (
                "reviewer",
                reviewer_response["provenance"],
                "semantic_reviewer_raw_request",
            ),
        )
        for label, provenance, role in cases:
            with self.subTest(label=label):
                self.assertEqual(provenance["session_id"], f"{role}-thread-jsonl-001")
                self.assertEqual(provenance["codex_event_id"], f"{role}-item-001")
                self.assertIn("item.completed", provenance["codex_event_types"])
        self.assertEqual(
            semantic_plan["planner_provenance"]["adapter_invocation"]["session_id"],
            "semantic_planner_raw_request-thread-jsonl-001",
        )
        self.assertEqual(
            oracle["oracle_provenance"]["session_id"],
            "semantic_oracle_raw_request-thread-jsonl-001",
        )
        self.assertEqual(
            review["reviewer_provenance"]["session_id"],
            "semantic_reviewer_raw_request-thread-jsonl-001",
        )
        persisted_validation = self.load_json(run_dir / "semantic_planner_validation.json")
        self.assertTrue(persisted_validation["ok"], persisted_validation)

    def test_codex_semantic_parent_overrides_model_mistyped_raw_request_hash(self) -> None:
        question = "Compare official public health poster images for handwashing guidance"

        def mistype_hash(response: dict) -> dict:
            response = json.loads(json.dumps(response))
            response["provenance"]["raw_request_hash"] = "model-mistyped-request-hash"
            return response

        result, adapter_request = self.prepare_with_codex_adapter(
            question,
            stdout_format="jsonl_item_text",
            response_mutator=mistype_hash,
            requirement_types=("subject", "source_quality", "visual_modality"),
            visual_angle_indexes=(2, 3),
        )
        run_dir = Path(result["run_dir"])
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")["semantic_plan"]
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )
        provenance = raw_response["provenance"]

        self.assertTrue(result["semantic_release_eligible"])
        self.assertEqual(result["planner_mode"], PLANNER_MODE_CODEX_SEMANTIC)
        self.assertEqual(
            provenance["raw_request_hash"],
            adapter_request["adapter_request_hash"],
        )
        self.assertEqual(
            provenance["child_reported_raw_request_hash"],
            "model-mistyped-request-hash",
        )
        self.assertTrue(provenance["raw_request_hash_overridden_by_parent"])
        self.assertEqual(
            semantic_plan["planner_provenance"]["adapter_invocation"]["raw_request_hash"],
            adapter_request["adapter_request_hash"],
        )
        self.assertEqual(
            semantic_plan["planner_provenance"]["adapter_invocation"][
                "child_reported_raw_request_hash"
            ],
            "model-mistyped-request-hash",
        )

    def test_codex_semantic_planner_validation_max_attempts_defaults_to_three_and_caps(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_codex_semantic_planner_validation_max_attempts(), 3)

        with mock.patch.dict(
            "os.environ",
            {CODEX_SEMANTIC_PLANNER_VALIDATION_MAX_ATTEMPTS_ENV: "1"},
            clear=True,
        ):
            self.assertEqual(_codex_semantic_planner_validation_max_attempts(), 1)

        with mock.patch.dict(
            "os.environ",
            {CODEX_SEMANTIC_PLANNER_VALIDATION_MAX_ATTEMPTS_ENV: "9"},
            clear=True,
        ):
            self.assertEqual(_codex_semantic_planner_validation_max_attempts(), 3)

        with mock.patch.dict(
            "os.environ",
            {CODEX_SEMANTIC_PLANNER_VALIDATION_MAX_ATTEMPTS_ENV: "invalid"},
            clear=True,
        ):
            self.assertEqual(_codex_semantic_planner_validation_max_attempts(), 3)

    def test_codex_semantic_rejects_effective_broad_narrow_self_label_before_reviewer(self) -> None:
        question = "Compare official product screenshots and chart images for onboarding workflows"

        result, adapter_request = self.prepare_with_codex_adapter(
            question,
            stdout_format="jsonl_item_text",
            question_scope="narrow",
            angle_count=4,
            tasks_per_angle=2,
            requirement_types=("subject", "source_quality", "visual_modality"),
            visual_angle_indexes=(2, 3),
        )

        self.assert_invalid_adapter_response_blocked(
            result,
            expected_failure_codes={
                "broad_question_angle_count_out_of_range",
                "broad_question_task_count_out_of_range",
            },
        )
        run_dir = Path(result["run_dir"])
        self.assertEqual(adapter_request["retry_attempt"], 3)
        self.assertFalse(
            (run_dir / "semantic_reviewer_raw" / "reviewer_request.json").exists()
        )
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )
        candidate_validation = raw_response["adapter_response"]["candidate_validation"]
        self.assertTrue(candidate_validation["effective_broad_question"])
        self.assertEqual(candidate_validation["declared_question_scope"], "narrow")
        self.assertEqual(candidate_validation["question_class"], "visual_style")
        self.assertEqual(candidate_validation["angle_count"], 4)
        self.assertEqual(candidate_validation["task_count"], 8)
        self.assertEqual(
            set(candidate_validation["expected_evidence_needs"]),
            {
                "primary_source",
                "visual_example",
                "visual_observation",
                "official_source",
            },
        )

    def test_validate_semantic_candidate_plan_accepts_valid_effective_broad_candidate(self) -> None:
        question = "Compare official product screenshots and chart images for onboarding workflows"
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "a" * 64,
        }
        response = self.codex_adapter_response(
            request,
            question_scope="broad",
            angle_count=5,
            tasks_per_angle=4,
            requirement_types=("subject", "source_quality", "visual_modality"),
            visual_angle_indexes=(2, 3),
        )

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=response["candidate_plan"],
        )

        self.assertTrue(validation["ok"], validation)
        self.assertTrue(validation["effective_broad_question"])
        self.assertEqual(validation["declared_question_scope"], "broad")
        self.assertEqual(validation["question_class"], "visual_style")
        self.assertEqual(validation["angle_count"], 5)
        self.assertEqual(validation["task_count"], 20)

    def test_validate_semantic_candidate_plan_rejects_shallow_release_angle_overlap(self) -> None:
        question = (
            "Compare flood-zone map images and textual guidance for homeowners "
            "using official sources, focusing on risk communication in a decision table"
        )
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "d" * 64,
        }
        response = self.codex_adapter_response(
            request,
            question_scope="broad",
            angle_count=5,
            tasks_per_angle=4,
            requirement_types=("subject", "source_quality", "visual_modality", "deliverable_shape"),
            visual_angle_indexes=(2, 3),
        )
        candidate = json.loads(json.dumps(response["candidate_plan"]))
        candidate["angles"][3]["title"] = "Homeowner Actionability"
        candidate["angles"][3]["research_question"] = (
            "Which modality better helps homeowners decide what to do?"
        )

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
        )

        self.assertFalse(validation["ok"], validation)
        failure = next(
            failure
            for failure in validation["failures"]
            if failure["code"] == "semantic_angle_release_depth_failed"
        )
        self.assertEqual(failure["angle_id"], "angle_004")
        self.assertIn("meaningful_overlap_too_low", failure["reasons"])
        self.assertEqual(failure["minimum_meaningful_overlap"], 2)
        self.assertLess(failure["meaningful_overlap_count"], 2)

    def test_validate_semantic_candidate_plan_accepts_repaired_release_angle_overlap(self) -> None:
        question = (
            "Compare flood-zone map images and textual guidance for homeowners "
            "using official sources, focusing on risk communication in a decision table"
        )
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "e" * 64,
        }
        response = self.codex_adapter_response(
            request,
            question_scope="broad",
            angle_count=5,
            tasks_per_angle=4,
            requirement_types=("subject", "source_quality", "visual_modality", "deliverable_shape"),
            visual_angle_indexes=(2, 3),
        )
        candidate = json.loads(json.dumps(response["candidate_plan"]))
        candidate["angles"][3]["title"] = (
            "Flood-Zone Map Images and Textual Guidance Risk Communication"
        )
        candidate["angles"][3]["research_question"] = (
            "How do flood-zone map images and textual guidance communicate risk "
            "to homeowners in official sources?"
        )

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
        )

        self.assertTrue(validation["ok"], validation)
        self.assertNotIn(
            "semantic_angle_release_depth_failed",
            {failure["code"] for failure in validation["failures"]},
        )

    def test_validate_semantic_candidate_plan_rejects_anchor_heavy_suffix_duplicate_release_angle(self) -> None:
        question = (
            "Compare 2026 Korea AI safety regulation enforcement guidance compliance "
            "matrix using official sources and agency notices"
        )
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "2" * 64,
        }
        response = self.codex_adapter_response(
            request,
            question_scope="broad",
            angle_count=5,
            tasks_per_angle=4,
            requirement_types=(
                "subject",
                "source_quality",
                "time_range",
                "geography",
                "deliverable_shape",
            ),
        )
        candidate = json.loads(json.dumps(response["candidate_plan"]))
        anchor_heavy_title = (
            "2026 Korea AI Safety Regulation Enforcement Guidance Compliance Matrix"
        )
        anchor_heavy_question = (
            "Which 2026 Korea AI safety regulation enforcement guidance compliance matrix?"
        )
        candidate["angles"][3]["title"] = anchor_heavy_title
        candidate["angles"][3]["research_question"] = anchor_heavy_question
        candidate["angles"][4]["title"] = f"{anchor_heavy_title} Update"
        candidate["angles"][4]["research_question"] = anchor_heavy_question

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
        )

        self.assertFalse(validation["ok"], validation)
        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertNotIn("semantic_angle_release_depth_failed", failure_codes)
        failure = next(
            failure
            for failure in validation["failures"]
            if failure["code"] == "semantic_angle_release_duplicate_failed"
        )
        self.assertEqual(failure["colliding_angle_ids"], ["angle_004", "angle_005"])
        duplicate_pair = failure["duplicate_pairs"][0]
        self.assertEqual(
            duplicate_pair["reason"],
            "contained_prompt_anchor_suffix_duplicate",
        )
        self.assertEqual(duplicate_pair["distinguishing_delta_tokens"], ["update"])
        self.assertEqual(duplicate_pair["substantive_distinguishing_tokens"], [])
        self.assertEqual(
            duplicate_pair["non_substantive_distinguishing_tokens"],
            ["update"],
        )

    def test_validate_semantic_candidate_plan_rejects_anchor_heavy_three_token_suffix_duplicate_release_angle(self) -> None:
        question = (
            "Compare 2026 Korea AI safety regulation enforcement guidance compliance "
            "matrix using official sources and agency notices"
        )
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "3" * 64,
        }
        response = self.codex_adapter_response(
            request,
            question_scope="broad",
            angle_count=5,
            tasks_per_angle=4,
            requirement_types=(
                "subject",
                "source_quality",
                "time_range",
                "geography",
                "deliverable_shape",
            ),
        )
        candidate = json.loads(json.dumps(response["candidate_plan"]))
        anchor_heavy_title = (
            "2026 Korea AI Safety Regulation Enforcement Guidance Compliance Matrix"
        )
        anchor_heavy_question = (
            "Which 2026 Korea AI safety regulation enforcement guidance compliance matrix?"
        )
        candidate["angles"][3]["title"] = anchor_heavy_title
        candidate["angles"][3]["research_question"] = anchor_heavy_question
        candidate["angles"][4]["title"] = (
            f"{anchor_heavy_title} Latest Status Update"
        )
        candidate["angles"][4]["research_question"] = anchor_heavy_question

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
        )

        self.assertFalse(validation["ok"], validation)
        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertNotIn("semantic_angle_release_depth_failed", failure_codes)
        failure = next(
            failure
            for failure in validation["failures"]
            if failure["code"] == "semantic_angle_release_duplicate_failed"
        )
        self.assertEqual(failure["colliding_angle_ids"], ["angle_004", "angle_005"])
        duplicate_pair = failure["duplicate_pairs"][0]
        self.assertEqual(
            duplicate_pair["reason"],
            "contained_prompt_anchor_suffix_duplicate",
        )
        self.assertEqual(
            duplicate_pair["distinguishing_delta_tokens"],
            ["latest", "status", "update"],
        )
        self.assertEqual(duplicate_pair["substantive_distinguishing_tokens"], [])
        self.assertEqual(
            duplicate_pair["non_substantive_distinguishing_tokens"],
            ["latest", "status", "update"],
        )

    def test_validate_semantic_candidate_plan_rejects_direct_and_reordered_release_angle_duplicates(self) -> None:
        question = (
            "Compare 2026 Korea AI safety regulation enforcement guidance compliance "
            "matrix using official sources and agency notices"
        )
        anchor_heavy_title = (
            "2026 Korea AI Safety Regulation Enforcement Guidance Compliance Matrix"
        )
        anchor_heavy_question = (
            "Which 2026 Korea AI safety regulation enforcement guidance compliance matrix?"
        )
        cases = {
            "direct": (anchor_heavy_title, anchor_heavy_question),
            "reordered": (
                "Compliance Matrix Guidance Enforcement Regulation Safety Korea 2026",
                "Which compliance matrix guidance enforcement regulation safety Korea 2026?",
            ),
        }
        for label, (right_title, right_question) in cases.items():
            with self.subTest(label=label):
                request = {
                    "original_question": question,
                    "depth_preset": "standard",
                    "planner_adapter": "codex_native_semantic_candidate_adapter",
                    "prompt_version": "p3-sp2-candidate-v2",
                    "adapter_request_hash": label[0] * 64,
                }
                response = self.codex_adapter_response(
                    request,
                    question_scope="broad",
                    angle_count=5,
                    tasks_per_angle=4,
                    requirement_types=(
                        "subject",
                        "source_quality",
                        "time_range",
                        "geography",
                        "deliverable_shape",
                    ),
                )
                candidate = json.loads(json.dumps(response["candidate_plan"]))
                candidate["angles"][3]["title"] = anchor_heavy_title
                candidate["angles"][3]["research_question"] = anchor_heavy_question
                candidate["angles"][4]["title"] = right_title
                candidate["angles"][4]["research_question"] = right_question

                validation = validate_semantic_candidate_plan(
                    original_question=question,
                    plan=candidate,
                )

                self.assertFalse(validation["ok"], validation)
                failure_codes = {failure["code"] for failure in validation["failures"]}
                self.assertNotIn("semantic_angle_release_depth_failed", failure_codes)
                failure = next(
                    failure
                    for failure in validation["failures"]
                    if failure["code"] == "semantic_angle_release_duplicate_failed"
                )
                self.assertEqual(
                    failure["colliding_angle_ids"],
                    ["angle_004", "angle_005"],
                )
                duplicate_pair = failure["duplicate_pairs"][0]
                self.assertEqual(duplicate_pair["reason"], "exact_token_signature")

    def test_validate_semantic_candidate_plan_rejects_deep_duplicate_release_angles(self) -> None:
        question = (
            "Compare 2026 Korea AI safety regulation enforcement guidance using "
            "official sources, court decisions, compliance deadlines, and a matrix deliverable"
        )
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "f" * 64,
        }
        response = self.codex_adapter_response(
            request,
            question_scope="broad",
            angle_count=5,
            tasks_per_angle=4,
            requirement_types=(
                "subject",
                "source_quality",
                "time_range",
                "geography",
                "deliverable_shape",
            ),
        )
        candidate = json.loads(json.dumps(response["candidate_plan"]))
        sem_reg_angles = [
            (
                "Statutory Authority and Agency Enforcement Channels",
                "Which Korea regulation agencies publish 2026 enforcement guidance for AI safety compliance?",
            ),
            (
                "Court Decisions and Appeal Precedents",
                "Which court decisions interpret Korea AI safety regulation obligations and enforcement disputes?",
            ),
            (
                "Compliance Deadlines and Transition Milestones",
                "Which 2026 deadlines and transition dates drive AI safety compliance sequencing?",
            ),
            (
                "Korea 2026 AI Safety Regulation Enforcement Guidance Deadlines",
                "Which 2026 Korea AI safety regulation enforcement deadlines and guidance constrain compliance matrix decisions?",
            ),
            (
                "2026 Korea AI Safety Regulation Enforcement Guidance Deadlines Update",
                "Which guidance and enforcement deadlines for Korea 2026 AI safety regulation constrain compliance matrix decisions?",
            ),
        ]
        for angle, (title, research_question) in zip(candidate["angles"], sem_reg_angles):
            angle["title"] = title
            angle["research_question"] = research_question

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
        )

        self.assertFalse(validation["ok"], validation)
        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertNotIn("semantic_angle_release_depth_failed", failure_codes)
        failure = next(
            failure
            for failure in validation["failures"]
            if failure["code"] == "semantic_angle_release_duplicate_failed"
        )
        self.assertEqual(failure["threshold"], 0.85)
        self.assertEqual(failure["colliding_angle_ids"], ["angle_004", "angle_005"])
        self.assertEqual(failure["colliding_angle_indexes"], [4, 5])
        duplicate_pair = failure["duplicate_pairs"][0]
        self.assertEqual(duplicate_pair["left_angle_id"], "angle_004")
        self.assertEqual(duplicate_pair["right_angle_id"], "angle_005")
        self.assertGreaterEqual(duplicate_pair["containment"], 0.85)
        self.assertIn("regulation", duplicate_pair["shared_tokens"])
        self.assertIn("regulation", duplicate_pair["shared_prompt_anchor_tokens"])
        self.assertGreaterEqual(
            duplicate_pair["distinguishing_containment"],
            0.85,
        )

    def test_validate_semantic_candidate_plan_accepts_distinct_anchor_heavy_release_angles(self) -> None:
        question = (
            "Compare 2026 Korea AI safety regulation enforcement guidance compliance "
            "sequencing using official sources, court decisions, compliance deadlines, "
            "and a matrix deliverable"
        )
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "1" * 64,
        }
        response = self.codex_adapter_response(
            request,
            question_scope="broad",
            angle_count=5,
            tasks_per_angle=4,
            requirement_types=(
                "subject",
                "source_quality",
                "time_range",
                "geography",
                "deliverable_shape",
            ),
        )
        candidate = json.loads(json.dumps(response["candidate_plan"]))
        sem_reg_angles = [
            (
                "Agency Publication Channels and Authority",
                "Which Korea agencies publish 2026 AI safety regulation enforcement guidance for compliance sequencing?",
                "official_source",
                ["official agency guidance", "regulatory notices"],
                "Agency Guidance Evidence",
            ),
            (
                "2026 Korea AI Safety Regulation Enforcement Guidance Compliance Sequencing Deadlines",
                "Which compliance deadlines shape 2026 Korea AI safety regulation enforcement guidance sequencing?",
                "recent_change",
                ["official compliance calendars", "agency transition schedules"],
                "Compliance Deadline Evidence",
            ),
            (
                "2026 Korea AI Safety Regulation Enforcement Guidance Compliance Sequencing Court Decisions",
                "Which court decisions shape 2026 Korea AI safety regulation enforcement guidance compliance sequencing?",
                "policy_or_legal",
                ["court decisions", "legal databases"],
                "Court Decision Evidence",
            ),
            (
                "Source Versioning and Update Traceability",
                "Which official sources show 2026 Korea AI safety guidance updates for compliance sequencing?",
                "primary_source",
                ["official source archives", "regulatory update pages"],
                "Version Traceability Evidence",
            ),
            (
                "Matrix Caveats and Evidence Weighting",
                "How should the matrix separate deadlines, court decisions, guidance updates, and caveats for Korea AI safety regulation?",
                "comparative_analysis",
                ["official guidance", "court decisions", "deadline schedules"],
                "Matrix Evidence Weighting",
            ),
        ]
        for angle, (
            title,
            research_question,
            evidence_need,
            expected_source_types,
            report_section,
        ) in zip(candidate["angles"], sem_reg_angles):
            angle["title"] = title
            angle["research_question"] = research_question
            angle["evidence_need"] = evidence_need
            angle["expected_source_types"] = expected_source_types
            angle["expected_artifacts"] = [f"{report_section} table"]
            angle["report_section"] = report_section

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
        )

        self.assertTrue(validation["ok"], validation)
        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertNotIn("semantic_angle_release_depth_failed", failure_codes)
        self.assertNotIn("semantic_angle_release_duplicate_failed", failure_codes)

    def test_validate_semantic_candidate_plan_accepts_distinct_release_angles(self) -> None:
        question = (
            "Compare 2026 Korea AI safety regulation enforcement guidance using "
            "official sources, court decisions, compliance deadlines, and a matrix deliverable"
        )
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "0" * 64,
        }
        response = self.codex_adapter_response(
            request,
            question_scope="broad",
            angle_count=5,
            tasks_per_angle=4,
            requirement_types=(
                "subject",
                "source_quality",
                "time_range",
                "geography",
                "deliverable_shape",
            ),
        )
        candidate = json.loads(json.dumps(response["candidate_plan"]))
        sem_reg_angles = [
            (
                "Statutory Authority and Agency Enforcement Channels",
                "Which Korea regulation agencies publish 2026 enforcement guidance for AI safety compliance?",
            ),
            (
                "Court Decisions and Appeal Precedents",
                "Which court decisions interpret Korea AI safety regulation obligations and enforcement disputes?",
            ),
            (
                "Compliance Deadlines and Transition Milestones",
                "Which 2026 deadlines and transition dates drive AI safety compliance sequencing?",
            ),
            (
                "Korea 2026 AI Safety Regulation Enforcement Guidance Deadlines",
                "Which 2026 Korea AI safety regulation enforcement deadlines and guidance constrain compliance matrix decisions?",
            ),
            (
                "Matrix Deliverable Evidence Weighting and Source Traceability",
                "How should the compliance matrix separate guidance, court decisions, deadlines, and risk caveats for AI safety?",
            ),
        ]
        for angle, (title, research_question) in zip(candidate["angles"], sem_reg_angles):
            angle["title"] = title
            angle["research_question"] = research_question

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
        )

        self.assertTrue(validation["ok"], validation)
        self.assertNotIn(
            "semantic_angle_release_duplicate_failed",
            {failure["code"] for failure in validation["failures"]},
        )

    def test_codex_semantic_retry_instructions_include_release_angle_repair_hint(self) -> None:
        question = (
            "Compare flood-zone map images and textual guidance for homeowners "
            "using official sources, focusing on risk communication in a decision table"
        )
        attempts = {"count": 0}

        def first_response_has_shallow_angle(response: dict) -> dict:
            attempts["count"] += 1
            response = json.loads(json.dumps(response))
            if attempts["count"] == 1:
                response["candidate_plan"]["angles"][3]["title"] = question
                response["candidate_plan"]["angles"][3]["research_question"] = (
                    "Which modality better helps homeowners decide what to do?"
                )
            return response

        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            stdout_format="jsonl_item_text",
            response_mutator=first_response_has_shallow_angle,
            requirement_types=("subject", "source_quality", "visual_modality", "deliverable_shape"),
            visual_angle_indexes=(2, 3),
        )
        run_dir = Path(result["run_dir"])
        raw_request = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_request.json"
        )
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )

        self.assertEqual(attempts["count"], 2)
        self.assertEqual(raw_response["adapter_attempt"], 2)
        self.assertIn(
            "semantic_angle_release_depth_failed",
            raw_request["previous_candidate_validation_failure_codes"],
        )
        instructions = raw_request["planner_retry_instructions"]
        self.assertIn("prompt-specific", instructions)
        self.assertIn("at least 2 meaningful non-generic tokens", instructions)
        self.assertIn("subject, modality, source-quality, geography/time", instructions)
        self.assertTrue(result["semantic_release_eligible"], result)

    def test_codex_semantic_materializes_shallow_angle_titles_with_prompt_anchors(self) -> None:
        question = "국내 개인정보 영향평가 제도와 해외 규제기관 가이드를 비교해줘."

        def sem_reg_006_style_shallow_titles(response: dict) -> dict:
            response = json.loads(json.dumps(response))
            response["candidate_plan"]["angles"][3]["title"] = (
                "동일 비교축에 따른 제도 대조"
            )
            response["candidate_plan"]["angles"][3]["research_question"] = (
                "선정 관할들은 법적 지위, 발동 요건, 평가 과정, 참여, "
                "감독기관 관여, 투명성 및 사후관리 측면에서 어떤 공통점과 "
                "차이를 보이는가?"
            )
            response["candidate_plan"]["angles"][4]["title"] = (
                "최신성·한계 검증과 국내 실무 시사점"
            )
            response["candidate_plan"]["angles"][4]["research_question"] = (
                "비교 결과에서 확인되는 최신 개정, 상충, 적용 한계는 "
                "무엇이며 국내 공공·민간 실무에 어떤 신중한 시사점을 "
                "도출할 수 있는가?"
            )
            return response

        raw_candidate = self.codex_adapter_response(
            build_codex_semantic_raw_request(question=question),
            requirement_types=("subject", "source_quality"),
        )
        raw_candidate = sem_reg_006_style_shallow_titles(raw_candidate)
        raw_validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=raw_candidate["candidate_plan"],
        )
        self.assertFalse(raw_validation["ok"], raw_validation)
        self.assertIn(
            "semantic_angle_release_depth_failed",
            {failure["code"] for failure in raw_validation["failures"]},
        )

        result, raw_request = self.prepare_with_codex_adapter(
            question,
            stdout_format="jsonl_item_text",
            response_mutator=sem_reg_006_style_shallow_titles,
            requirement_types=("subject", "source_quality"),
        )
        run_dir = Path(result["run_dir"])
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")[
            "semantic_plan"
        ]
        repaired_validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=semantic_plan,
        )

        self.assertEqual(raw_request["budget_cap"]["max_results"], 8)
        self.assertTrue(result["semantic_release_eligible"], result)
        self.assertTrue(repaired_validation["ok"], repaired_validation)
        materializations = raw_response[
            "candidate_plan_angle_title_materializations"
        ]
        self.assertEqual(
            [item["angle_id"] for item in materializations],
            ["angle_004", "angle_005"],
        )
        self.assertIn("국내 개인정보", materializations[0]["materialized_title"])
        self.assertIn("국내 개인정보 영향평가", materializations[1]["materialized_title"])
        self.assertEqual(
            materializations[0]["materialization"],
            "prepended_prompt_anchor_tokens",
        )
        failure_codes = {
            failure["code"] for failure in repaired_validation["failures"]
        }
        self.assertNotIn("semantic_angle_release_depth_failed", failure_codes)

    def test_codex_semantic_retry_instructions_include_duplicate_angle_repair_hint(self) -> None:
        question = (
            "Compare 2026 Korea AI safety regulation enforcement guidance using "
            "official sources, court decisions, compliance deadlines, and a matrix deliverable"
        )
        attempts = {"count": 0}

        def first_response_has_duplicate_angle(response: dict) -> dict:
            attempts["count"] += 1
            response = json.loads(json.dumps(response))
            if attempts["count"] == 1:
                duplicate_source = response["candidate_plan"]["angles"][3]
                duplicate_target = response["candidate_plan"]["angles"][4]
                duplicate_target["title"] = f"{duplicate_source['title']} Update"
                duplicate_target["research_question"] = duplicate_source[
                    "research_question"
                ]
            return response

        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            stdout_format="jsonl_item_text",
            response_mutator=first_response_has_duplicate_angle,
            requirement_types=(
                "subject",
                "source_quality",
                "time_range",
                "geography",
                "deliverable_shape",
            ),
        )
        run_dir = Path(result["run_dir"])
        raw_request = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_request.json"
        )
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )

        self.assertEqual(attempts["count"], 2)
        self.assertEqual(raw_response["adapter_attempt"], 2)
        self.assertIn(
            "semantic_angle_release_duplicate_failed",
            raw_request["previous_candidate_validation_failure_codes"],
        )
        instructions = raw_request["planner_retry_instructions"]
        self.assertIn("materially distinct semantic angles", instructions)
        self.assertIn("numeric suffixes", instructions)
        self.assertIn("reordered phrasing", instructions)
        self.assertTrue(result["semantic_release_eligible"], result)

    def test_codex_semantic_retries_candidate_validation_failures_until_third_attempt(self) -> None:
        question = "Compare official public health poster images for handwashing guidance"
        attempts = {"count": 0}

        def first_two_responses_underallocate_one_broad_angle(response: dict) -> dict:
            attempts["count"] += 1
            response = json.loads(json.dumps(response))
            if attempts["count"] > 2:
                return response
            candidate = response["candidate_plan"]
            underallocated_angle_id = "angle_006"
            seen_underallocated_angle_task = False
            kept_tasks = []
            for task in candidate["bounded_tasks"]:
                if task.get("angle_id") != underallocated_angle_id:
                    kept_tasks.append(task)
                    continue
                if not seen_underallocated_angle_task:
                    kept_tasks.append(task)
                    seen_underallocated_angle_task = True
            kept_task_ids = {task["task_id"] for task in kept_tasks}
            candidate["bounded_tasks"] = kept_tasks
            for requirement in candidate["requirement_coverage_map"]:
                requirement["covered_by_task_ids"] = [
                    task_id
                    for task_id in requirement["covered_by_task_ids"]
                    if task_id in kept_task_ids
                ]
            return response

        result, adapter_request = self.prepare_with_codex_adapter(
            question,
            stdout_format="jsonl_item_text",
            response_mutator=first_two_responses_underallocate_one_broad_angle,
            angle_count=6,
            requirement_types=("subject", "source_quality", "visual_modality"),
            visual_angle_indexes=(2, 3),
        )
        run_dir = Path(result["run_dir"])
        raw_request = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_request.json"
        )
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")["semantic_plan"]

        self.assertEqual(attempts["count"], 3)
        self.assertTrue(result["semantic_release_eligible"])
        self.assertEqual(result["planner_mode"], PLANNER_MODE_CODEX_SEMANTIC)
        self.assertEqual(raw_response["adapter_attempt"], 3)
        self.assertEqual(raw_response["adapter_attempt_count"], 3)
        self.assertEqual(len(raw_response["previous_adapter_attempts"]), 2)
        self.assertIn(
            "broad_angle_has_too_few_tasks",
            raw_response["previous_adapter_attempts"][0][
                "candidate_validation_failure_codes"
            ],
        )
        self.assertIn(
            "broad_angle_has_too_few_tasks",
            raw_response["previous_adapter_attempts"][1][
                "candidate_validation_failure_codes"
            ],
        )
        self.assertEqual(raw_request["retry_attempt"], 3)
        self.assertIn(
            "broad_angle_has_too_few_tasks",
            raw_request["previous_candidate_validation_failure_codes"],
        )
        self.assertIn(
            "broad_angle_has_too_few_tasks",
            raw_request["planner_retry_instructions"],
        )
        self.assertIn(
            "at least 2 bounded_tasks assigned to every angle_id",
            raw_request["planner_retry_instructions"],
        )
        self.assertIn(
            "per-angle task distribution",
            raw_request["planner_retry_instructions"],
        )
        self.assertTrue(
            any(
                failure.get("code") == "broad_angle_has_too_few_tasks"
                and failure.get("angle_id") == "angle_006"
                for failure in raw_request["previous_candidate_validation_failures"]
            ),
            raw_request["previous_candidate_validation_failures"],
        )
        self.assertEqual(
            raw_response["provenance"]["raw_request_hash"],
            raw_request["adapter_request_hash"],
        )
        self.assertEqual(
            semantic_plan["planner_provenance"]["adapter_request_hash"],
            raw_request["adapter_request_hash"],
        )
        self.assertEqual(adapter_request["retry_attempt"], 3)

    def test_codex_semantic_independence_qualifies_reused_item_ids_by_session(self) -> None:
        question = "Research coastal microgrid outage recovery using official source records"
        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            stdout_format="jsonl_item_text_reused_item_id",
            requirement_types=("subject", "source_quality"),
        )
        run_dir = Path(result["run_dir"])
        review = self.load_json(run_dir / "semantic_plan_review.json")

        self.assertTrue(
            review["reviewer_independence"]["independent"],
            review["reviewer_independence"],
        )
        self.assertFalse(
            review["reviewer_independence"]["reviewer_oracle_shared_provenance"],
            review["reviewer_independence"],
        )
        self.assertIn(
            "codex_event_id:semantic_oracle_raw_request-thread-jsonl-001:item_0",
            review["reviewer_independence"]["oracle_identity"],
        )
        self.assertIn(
            "codex_event_id:semantic_reviewer_raw_request-thread-jsonl-001:item_0",
            review["reviewer_independence"]["reviewer_identity"],
        )

    def test_codex_semantic_unavailable_blocks_without_local_template_fallback(self) -> None:
        question = "Research adapter unavailable behavior without manual angles"
        with mock.patch("deepresearch.semantic_planner.shutil.which", return_value=None):
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

    def test_default_codex_semantic_adapter_command_uses_plugin_schema(self) -> None:
        with mock.patch(
            "deepresearch.semantic_planner.shutil.which",
            return_value="/usr/local/bin/codex",
        ), mock.patch.dict(
            "os.environ",
            {
                CODEX_SEMANTIC_ENABLE_DEFAULT_ADAPTER_ENV: "1",
                CODEX_SEMANTIC_ADAPTER_WORKDIR_ENV: str(ROOT),
                CODEX_SEMANTIC_PLANNER_COMMAND_ENV: "",
            },
            clear=False,
        ):
            command = _semantic_adapter_command(
                command_env=CODEX_SEMANTIC_PLANNER_COMMAND_ENV,
                role_label="semantic planner",
            )

        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command[:3], ["/usr/local/bin/codex", "exec", "--json"])
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--output-schema", command)
        schema_path = Path(command[command.index("--output-schema") + 1])
        self.assertTrue(schema_path.exists())
        self.assertEqual(schema_path.name, "planner.json")
        self.assertIn("semantically, not by keyword or fixed template", command[-1])

    def test_default_codex_semantic_adapter_command_returns_none_without_codex(self) -> None:
        with mock.patch(
            "deepresearch.semantic_planner.shutil.which",
            return_value=None,
        ), mock.patch.dict(
            "os.environ",
            {
                CODEX_SEMANTIC_ENABLE_DEFAULT_ADAPTER_ENV: "1",
                CODEX_SEMANTIC_PLANNER_COMMAND_ENV: "",
            },
            clear=False,
        ):
            command = _semantic_adapter_command(
                command_env=CODEX_SEMANTIC_PLANNER_COMMAND_ENV,
                role_label="semantic planner",
            )

        self.assertIsNone(command)

    def test_default_codex_semantic_adapter_command_can_be_disabled_for_tests(self) -> None:
        with mock.patch(
            "deepresearch.semantic_planner.shutil.which",
            return_value="/usr/local/bin/codex",
        ), mock.patch.dict(
            "os.environ",
            {
                CODEX_SEMANTIC_ENABLE_DEFAULT_ADAPTER_ENV: "1",
                "CODEX_DEEPRESEARCH_DISABLE_DEFAULT_SEMANTIC_ADAPTER": "1",
                CODEX_SEMANTIC_PLANNER_COMMAND_ENV: "",
            },
            clear=False,
        ):
            command = _semantic_adapter_command(
                command_env=CODEX_SEMANTIC_PLANNER_COMMAND_ENV,
                role_label="semantic planner",
            )

        self.assertIsNone(command)

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

    def test_codex_semantic_invalid_broad_candidates_block_before_materialization(self) -> None:
        question = "2026 Korea battery safety image evidence from official sources as a table"

        def remove_visual_routes(response: dict) -> dict:
            response = json.loads(json.dumps(response))
            for angle in response["candidate_plan"]["angles"]:
                angle["route"] = "text_only"
                angle["expected_visual_targets"] = []
            for task in response["candidate_plan"]["bounded_tasks"]:
                task["route"] = "text_only"
                task["expected_visual_targets"] = []
                task["max_images"] = 0
            return response

        def drift_to_wrong_subject(response: dict) -> dict:
            response = json.loads(json.dumps(response))
            for angle in response["candidate_plan"]["angles"]:
                angle["title"] = "Codex runner architecture"
                angle["research_question"] = "How should Codex runner internals be implemented?"
                angle["why_this_angle_matters"] = (
                    "This generic implementation angle is out of scope."
                )
                angle["search_queries"] = ["Codex runner architecture implementation"]
                angle["included_scope"] = ["Codex implementation"]
                angle["expected_source_types"] = ["repository implementation notes"]
                angle["expected_visual_targets"] = []
                angle["expected_artifacts"] = ["implementation checklist"]
                angle["success_criteria"] = [
                    "Findings must explain Codex runner internals."
                ]
            for task in response["candidate_plan"]["bounded_tasks"]:
                task["query"] = "Codex runner architecture implementation and test strategy"
                task["success_criteria"] = [
                    "Findings must explain Codex runner internals."
                ]
                task["expected_artifacts"] = ["implementation checklist"]
                task["expected_source_types"] = ["repository implementation notes"]
                task["expected_visual_targets"] = []
                task["done_condition"] = (
                    "Stop after documenting Codex runner implementation details."
                )
            return response

        cases = (
            (
                "no_visual",
                remove_visual_routes,
                {"visual_requirement_missing_visual_route"},
            ),
            ("wrong_subject", drift_to_wrong_subject, {"subject_requirement_drift"}),
        )
        for case_name, mutator, expected_codes in cases:
            with self.subTest(case=case_name):
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
                    visual_angle_indexes=(2, 3),
                    response_mutator=mutator,
                )

                self.assert_invalid_adapter_response_blocked(
                    result,
                    expected_failure_codes=expected_codes,
                )

    def test_codex_semantic_malformed_nested_adapter_output_blocks_cleanly(self) -> None:
        def remove_nested_fields(response: dict) -> dict:
            response = json.loads(json.dumps(response))
            response["candidate_plan"]["angles"][0].pop("title")
            response["candidate_plan"]["bounded_tasks"][0].pop("query")
            return response

        result, _adapter_request = self.prepare_with_codex_adapter(
            "Research public water system resilience with official records",
            requirement_types=("subject", "source_quality"),
            response_mutator=remove_nested_fields,
        )

        self.assert_invalid_adapter_response_blocked(
            result,
            expected_failure_codes={
                "semantic_angle_missing_field",
                "bounded_task_missing_field",
            },
        )

    def test_codex_semantic_multi_source_caps_normalized_before_review_and_fanout(self) -> None:
        question = (
            "Korea EV battery fire safety standards and official recall records "
            "비교 최신 상충 caveat 조사"
        )
        multi_source_tasks = {
            "task_semantic_001": {
                "query": (
                    "Compare official ministry recall records against regulatory "
                    "database entries for Korea EV battery fire safety."
                ),
                "freshness_requirement": "any",
                "expected_source_types": [
                    "official ministry recall notice",
                    "regulatory recall database record",
                ],
                "source_policy": {
                    "decision": "allowed",
                    "allow_secondary": False,
                    "required_source_quality": [
                        "official ministry record",
                        "regulatory database record",
                    ],
                    "flags": [],
                },
                "success_criteria": [
                    "Compare the official and regulatory records before making claims.",
                    "Use official, regulatory, or primary sources for support.",
                ],
                "done_condition": "Stop after cross-checking both official records.",
            },
            "task_semantic_002": {
                "query": "공식 리콜 자료와 규제 고시를 비교하고 최신 개정 여부를 확인하라.",
                "freshness_requirement": "recent",
                "expected_source_types": ["공식 리콜 기록", "규제 고시 원문"],
                "source_policy": {
                    "decision": "allowed",
                    "allow_secondary": False,
                    "required_source_quality": ["공식 기록", "규제 원문"],
                    "flags": [],
                },
                "success_criteria": [
                    "최신 개정 여부와 공식 근거를 함께 기록한다.",
                    "Use official, regulatory, or primary sources for support.",
                ],
                "done_condition": "상충 또는 정정 사항이 확인되면 근거와 함께 완료한다.",
            },
            "task_semantic_003": {
                "query": (
                    "Audit contradiction, caveat, correction, and unresolved limits "
                    "across primary recall records."
                ),
                "freshness_requirement": "any",
                "expected_source_types": ["primary recall record", "official correction notice"],
                "source_policy": {
                    "decision": "allowed",
                    "allow_secondary": False,
                    "required_source_quality": [
                        "primary recall record",
                        "official correction notice",
                    ],
                    "flags": [],
                },
                "success_criteria": [
                    "Record contradiction caveats and unresolved limits.",
                    "Use official, regulatory, or primary sources for support.",
                ],
                "done_condition": "Stop after contradiction and caveat checks cite both records.",
            },
            "task_semantic_004": {
                "query": (
                    "Compare latest official, regulatory, primary, database, notice, "
                    "statute, and correction records for source-cap clamping."
                ),
                "freshness_requirement": "recent",
                "expected_source_types": [
                    "official record",
                    "regulatory record",
                    "primary record",
                    "database record",
                    "correction notice",
                    "statute record",
                ],
                "source_policy": {
                    "decision": "allowed",
                    "allow_secondary": False,
                    "required_source_quality": [
                        "official record",
                        "regulatory record",
                        "primary record",
                        "database record",
                        "correction notice",
                        "statute record",
                    ],
                    "flags": [],
                },
                "success_criteria": [
                    "Confirm latestness and correction status across official records.",
                    "Use official, regulatory, or primary sources for support.",
                ],
                "done_condition": "Stop after the latest record set is cross-checked.",
            },
        }

        def lower_multi_source_caps(response: dict) -> dict:
            response = json.loads(json.dumps(response))
            angle_specs = [
                (
                    "official recall baseline",
                    "Which official recall records establish the Korea EV battery fire safety baseline?",
                ),
                (
                    "regulatory amendment latestness",
                    "What latest regulatory amendments change the Korean battery safety standard?",
                ),
                (
                    "primary record contradiction",
                    "Where do primary recall records conflict with agency safety explanations?",
                ),
                (
                    "source caveat audit",
                    "Which unresolved caveats limit conclusions about recall evidence quality?",
                ),
                (
                    "comparison table synthesis",
                    "How should standards and recall records be organized into a comparison table?",
                ),
            ]
            for index, angle in enumerate(response["candidate_plan"]["angles"], start=1):
                focus, research_question = angle_specs[index - 1]
                angle["route"] = "text_only"
                angle["expected_visual_targets"] = []
                angle["evidence_need"] = "primary_source"
                angle["title"] = f"{question} {focus}"
                angle["research_question"] = research_question
                angle["why_this_angle_matters"] = (
                    "This angle keeps the text-only source-cap normalization fixture on subject."
                )
                angle["included_scope"] = [question, focus]
                angle["excluded_scope"] = ["Visual inspection and image evidence."]
                angle["expected_source_types"] = ["official source records"]
                angle["expected_artifacts"] = [f"{focus} notes"]
                angle["search_queries"] = [f"{question} {focus} official sources"]
                angle["success_criteria"] = [
                    "Findings must cite official source metadata."
                ]
                angle["report_section"] = f"Source Cap {index}"
                angle["risk_or_contradiction_checks"] = [
                    "Check whether source caps permit required cross-checks."
                ]
            for task in response["candidate_plan"]["bounded_tasks"]:
                task["route"] = "text_only"
                task["expected_visual_targets"] = []
                task["max_images"] = 0
                task["max_sources"] = 1
                task["freshness_requirement"] = "any"
                task["query"] = f"{question} single source bounded task {task['task_id']}"
                task["expected_source_types"] = ["single source note"]
                task["expected_artifacts"] = ["source-backed note"]
                task["source_policy"] = {"decision": "allowed", "flags": []}
                task["success_criteria"] = [
                    f"Task must preserve the subject: {question}."
                ]
                task["done_condition"] = "Stop after one source-backed note is recorded."
                if task["task_id"] in multi_source_tasks:
                    task.update(multi_source_tasks[task["task_id"]])
            return response

        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            requirement_types=("subject", "source_quality", "time_range"),
            response_mutator=lower_multi_source_caps,
        )
        run_dir = Path(result["run_dir"])
        self.assertEqual(result["semantic_planning_status"], "semantic_review_passed")
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")["semantic_plan"]
        search_tasks = self.load_json(run_dir / "search_tasks.json")["tasks"]
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )
        plan_by_id = {task["task_id"]: task for task in semantic_plan["bounded_tasks"]}
        search_by_id = {task["task_id"]: task for task in search_tasks}

        self.assertIn("candidate_plan_source_cap_normalizations", raw_response)
        normalized_ids = {
            record["task_id"]
            for record in raw_response["candidate_plan_source_cap_normalizations"]
        }
        self.assertTrue(set(multi_source_tasks).issubset(normalized_ids))
        for task in semantic_plan["bounded_tasks"]:
            self.assertGreaterEqual(task["max_sources"], 1)
            self.assertLessEqual(task["max_sources"], 5)
            if task["route"] == "text_only":
                self.assertEqual(task["max_images"], 0)
        for task_id in multi_source_tasks:
            with self.subTest(task_id=task_id):
                plan_task = plan_by_id[task_id]
                search_task = search_by_id[task_id]
                self.assertGreater(plan_task["max_sources"], 1)
                self.assertLessEqual(plan_task["max_sources"], 5)
                self.assertEqual(search_task["max_sources"], plan_task["max_sources"])
                self.assertEqual(plan_task["max_images"], 0)
                self.assertEqual(search_task["max_images"], 0)
        self.assertEqual(plan_by_id["task_semantic_004"]["max_sources"], 5)

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
            "max_results",
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
            "max_results",
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

    def test_codex_semantic_materializes_request_max_results_cap_into_plan_and_lineage(self) -> None:
        question = "Research official public health poster image guidance in Korea"
        result, adapter_request = self.prepare_with_codex_adapter(
            question,
            requirement_types=("subject", "visual_modality", "source_quality"),
            visual_angle_indexes=(2, 3),
        )
        run_dir = Path(result["run_dir"])
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")["semantic_plan"]
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )
        search_tasks = self.load_json(run_dir / "search_tasks.json")["tasks"]

        self.assertEqual(adapter_request["budget_cap"]["max_results"], 8)
        constraint_text = json.dumps(
            semantic_plan["constraints"],
            ensure_ascii=False,
        )
        self.assertIn("max_results=8", constraint_text)
        self.assertIn("candidate_plan_budget_cap_materializations", raw_response)
        self.assertTrue(semantic_plan["bounded_tasks"])
        self.assertTrue(search_tasks)
        for bounded_task, search_task in zip(
            semantic_plan["bounded_tasks"],
            search_tasks,
        ):
            self.assertEqual(bounded_task["max_results"], 8)
            self.assertEqual(search_task["max_results"], 8)
            self.assertEqual(search_task["max_sources"], bounded_task["max_sources"])

        planned = plan_research_tasks(run=run_dir, min_tasks=1)
        research_tasks = planned["tasks"]
        self.assertEqual(len(research_tasks), len(semantic_plan["bounded_tasks"]))
        for bounded_task, research_task in zip(
            semantic_plan["bounded_tasks"],
            research_tasks,
        ):
            self.assertEqual(research_task["max_results"], 8)
            self.assertEqual(research_task["max_sources"], bounded_task["max_sources"])

        diff = self.load_json(run_dir / "semantic_materialization_diff.json")
        self.assertTrue(diff["valid"], diff)
        search_check = next(
            check
            for check in diff["artifact_checks"]
            if check["artifact"] == "search_tasks"
        )
        self.assertIn("max_results", search_check["compared_fields"])

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
            visual_angle_indexes=(2, 3),
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

        visual_budget_zero = json.loads(json.dumps(plan))
        visual_task = next(
            task
            for task in visual_budget_zero["bounded_tasks"]
            if task["route"] != "text_only"
            or task.get("expected_visual_targets")
            or any(
                token in artifact.lower()
                for artifact in task.get("expected_artifacts", [])
                for token in ("image", "visual", "screenshot", "chart", "figure", "diagram")
            )
        )
        visual_task["max_images"] = 0
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=visual_budget_zero,
        )
        self.assertFalse(validation["ok"])
        self.assertIn(
            "visual_expected_evidence_without_image_budget",
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
                with mock.patch("deepresearch.semantic_planner.shutil.which", return_value=None):
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
            visual_angle_indexes=(2, 3),
        )
        run_dir = Path(result["run_dir"])
        evidence = self.load_json(run_dir / "evidence.json")
        validation = self.load_json(run_dir / "semantic_planner_validation.json")
        review = self.load_json(run_dir / "semantic_plan_review.json")
        plan_artifact = self.load_json(run_dir / "semantic_plan.json")
        semantic_plan = plan_artifact["semantic_plan"]

        self.assertEqual(evidence["semantic_planner"]["planner_mode"], PLANNER_MODE_CODEX_SEMANTIC)
        self.assertTrue(evidence["semantic_planner"]["semantic_release_eligible"])
        self.assertTrue(validation["ok"], validation)
        self.assertEqual(
            evidence["semantic_planner"]["status"],
            "semantic_review_passed",
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
        self.assertTrue(review["substitute_implementation_check"]["passed"])
        self.assertGreaterEqual(review.get("semantic_fit_score"), SEMANTIC_FIT_SCORE_THRESHOLD)
        self.assertEqual(review["verdict"], "pass")

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

        self.assertEqual(result["semantic_planning_status"], "semantic_review_passed")
        self.assertEqual(semantic_plan["planner_mode"], PLANNER_MODE_CODEX_SEMANTIC)
        self.assertTrue(semantic_plan["semantic_release_eligible"])
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
