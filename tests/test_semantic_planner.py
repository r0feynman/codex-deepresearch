from __future__ import annotations

import json
import hashlib
import os
import shlex
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
    CODEX_SEMANTIC_ADAPTER_CAPACITY_RETRY_BACKOFF_SECONDS_ENV,
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
    SEMANTIC_PLANNER_CONVERGENCE_FILENAME,
    SemanticAngle,
    SemanticPlan,
    SEMANTIC_MATERIALIZATION_ALIGNMENT_FIELDS,
    SEMANTIC_MATERIALIZATION_PLAN_HASH_FIELD,
    SEMANTIC_MATERIALIZATION_SEARCH_RESULT_FIELD_PREFIX,
    SemanticPlannerAdapterUnavailable,
    build_codex_semantic_raw_request,
    build_semantic_materialization_diff,
    classify_question,
    heuristic_template_planner,
    question_mentions_visual_evidence,
    semantic_materialization_plan_hash_for_tasks,
    semantic_planner_validation,
    validate_semantic_candidate_plan,
    validate_codex_semantic_adapter_provenance,
    write_semantic_materialization_diff,
    write_semantic_planner_validation,
    _codex_semantic_candidate_validation,
    _codex_semantic_planner_validation_max_attempts,
    _has_forbidden_internal_leakage,
    _materialize_candidate_budget_caps,
    _materialize_candidate_broad_cardinality,
    _materialize_candidate_placeholder_selection_workflow,
    _materialize_candidate_report_sections,
    _materialize_candidate_req003_comparison_deliverable,
    _materialize_candidate_source_cap_splits,
    _materialize_candidate_source_cap_constraints,
    _materialize_candidate_task_source_cap_feasibility,
    _materialize_candidate_visual_image_cap_feasibility,
    _normalize_candidate_executable_source_caps,
    _repair_candidate_requirement_coverage,
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

    def semantic_regression_prompt(self, index: int) -> str:
        manifest = self.load_json(
            ROOT
            / "plugins"
            / "codex-deepresearch"
            / "validation"
            / "semantic_regression_prompts.json"
        )
        return str(manifest["prompts"][index]["prompt"])

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

    def write_fake_codex_executable(self, bin_dir: Path) -> Path:
        fake_codex = bin_dir / "codex"
        fixture = ROOT / "tests" / "fixtures" / "semantic_scope_downgrade_adapter.py"
        fake_codex.write_text(
            "#!/usr/bin/env sh\n"
            f"exec {shlex.quote(sys.executable)} {shlex.quote(str(fixture))} \"$@\"\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        return fake_codex

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

    def test_semantic_materialization_diff_uses_stable_plan_hash(self) -> None:
        run_dir = self.write_semantic_materialization_fixture(visual=False)
        plan = self.load_json(run_dir / "semantic_plan.json")
        tasks = plan["semantic_plan"]["bounded_tasks"]
        stable_hash = semantic_materialization_plan_hash_for_tasks(tasks)
        plan[SEMANTIC_MATERIALIZATION_PLAN_HASH_FIELD] = stable_hash
        self.write_json(run_dir / "semantic_plan.json", plan)

        self.realign_semantic_materialization_fixture(run_dir)
        for artifact_name in ("search_tasks.json", "research_tasks.json"):
            payload = self.load_json(run_dir / artifact_name)
            for record in payload["tasks"]:
                record["semantic_plan_hash"] = stable_hash
            self.write_json(run_dir / artifact_name, payload)
        evidence = self.load_json(run_dir / "evidence.json")
        for record in evidence["search_tasks"]:
            record["semantic_plan_hash"] = stable_hash
        self.write_json(run_dir / "evidence.json", evidence)
        for artifact_name in ("search_results.jsonl", "subagent_assignments.jsonl"):
            records = self.read_jsonl(run_dir / artifact_name)
            for record in records:
                record["semantic_plan_hash"] = stable_hash
            self.write_jsonl(run_dir / artifact_name, records)

        mutated_plan = self.load_json(run_dir / "semantic_plan.json")
        mutated_plan["semantic_release_eligible"] = False
        mutated_plan["semantic_planner_validation_ok"] = False
        mutated_plan["semantic_planner_validation_failure_codes"] = [
            "post_materialization_validation_failure"
        ]
        self.write_json(run_dir / "semantic_plan.json", mutated_plan)

        diff = build_semantic_materialization_diff(
            run_dir=run_dir,
            require_downstream=True,
        )

        self.assertTrue(diff["valid"], diff)
        self.assertEqual(diff["semantic_plan_hash"], stable_hash)

    def test_requirement_coverage_repair_adds_visual_consistency_tasks(self) -> None:
        candidate = {
            "requirement_coverage_map": [
                {
                    "requirement_id": "req_004",
                    "requirement_text": (
                        "대한민국 공공건축을 잠정 범위로 삼고, 최신 유효 기준과 "
                        "입찰 시점의 문서 버전을 구분하며, 필요한 경우 모델·도면의 "
                        "시각 정합성을 선택적으로 검토한다."
                    ),
                    "requirement_type": "modality_geography_time_scope_filters",
                    "non_negotiable": True,
                    "coverage_status": "covered",
                    "covered_by_angle_ids": ["angle_001"],
                    "covered_by_task_ids": ["task_002"],
                }
            ],
            "bounded_tasks": [
                {
                    "task_id": "task_002",
                    "angle_id": "angle_001",
                    "route": "text_only",
                    "query": "건축 BIM 모델의 객체 분류와 속성 기준을 확인한다.",
                    "expected_visual_targets": [],
                },
                {
                    "task_id": "task_003",
                    "angle_id": "angle_001",
                    "route": "visual_optional",
                    "query": "공공 설계 성과품에서 모델과 도면 사이의 정합성 점검 항목을 수집한다.",
                    "expected_visual_targets": ["모델 뷰와 평면·입면·단면 대응"],
                },
                {
                    "task_id": "task_016",
                    "angle_id": "angle_004",
                    "route": "visual_optional",
                    "query": "제공 가능한 건축 모델 뷰와 도면을 사용해 시각 정합성 증거를 수집한다.",
                    "expected_visual_targets": ["모델과 평면의 공간·벽체·개구부 대응"],
                },
            ],
        }

        repaired, materializations = _repair_candidate_requirement_coverage(candidate)

        coverage = repaired["requirement_coverage_map"][0]
        self.assertEqual(
            set(coverage["covered_by_task_ids"]),
            {"task_002", "task_003", "task_016"},
        )
        self.assertIn("angle_004", coverage["covered_by_angle_ids"])
        self.assertEqual(materializations[0]["requirement_id"], "req_004")
        self.assertEqual(
            set(materializations[0]["added_task_ids"]),
            {"task_003", "task_016"},
        )

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

    def semantic_plan_from_candidate_payload(self, candidate: dict) -> SemanticPlan:
        def string_items(value: object) -> list[str]:
            return [str(item) for item in value] if isinstance(value, list) else []

        angles = [
            SemanticAngle(
                angle_id=str(angle["angle_id"]),
                title=str(angle["title"]),
                research_question=str(angle["research_question"]),
                question_context=str(
                    angle.get("question_context")
                    or (angle.get("included_scope") or [""])[0]
                ),
                route=str(angle["route"]),
                evidence_need=str(angle["evidence_need"]),
                expected_artifacts=string_items(angle.get("expected_artifacts")),
                success_criteria=string_items(angle.get("success_criteria")),
                report_section=str(angle["report_section"]),
                why_this_angle_matters=str(angle.get("why_this_angle_matters") or ""),
                included_scope=string_items(angle.get("included_scope")),
                excluded_scope=string_items(angle.get("excluded_scope")),
                expected_source_types=string_items(angle.get("expected_source_types")),
                expected_visual_targets=string_items(angle.get("expected_visual_targets")),
                search_queries=string_items(angle.get("search_queries")),
                risk_or_contradiction_checks=string_items(
                    angle.get("risk_or_contradiction_checks")
                ),
            )
            for angle in candidate["angles"]
        ]
        return SemanticPlan(
            schema_version=str(candidate["schema_version"]),
            question_class="product_market",
            broad_question=str(candidate["question_scope"]) == "broad",
            source=str(candidate.get("source") or "codex_semantic"),
            expected_evidence_needs=list(
                dict.fromkeys(angle.evidence_need for angle in angles)
            ),
            angles=angles,
            intent_summary=str(candidate["intent_summary"]),
            domain_entities=list(candidate.get("domain_entities") or []),
            constraints=list(candidate.get("constraints") or []),
            runner_source_budget=dict(candidate.get("runner_source_budget") or {}),
            question_scope=str(candidate["question_scope"]),
            scope_downgrade=(
                dict(candidate["scope_downgrade"])
                if isinstance(candidate.get("scope_downgrade"), dict)
                else None
            ),
            decomposition_strategy=str(candidate["decomposition_strategy"]),
            requirement_coverage_map=list(candidate["requirement_coverage_map"]),
            negative_scope=list(candidate.get("negative_scope") or []),
            bounded_tasks=list(candidate["bounded_tasks"]),
            planner_mode=PLANNER_MODE_CODEX_SEMANTIC,
            semantic_release_eligible=False,
        )

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
                    "min": (
                        20
                        if question_scope == "broad"
                        else 10
                        if question_scope == "medium"
                        else 6
                    ),
                    "max": (
                        40
                        if question_scope == "broad"
                        else 19
                        if question_scope == "medium"
                        else 12
                    ),
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
        capacity_failures_by_artifact: dict[str, int] | None = None,
        non_capacity_failures_by_artifact: dict[str, int] | None = None,
        planner_response_mutator: object | None = None,
        reviewer_response_mutator: object | None = None,
        **adapter_kwargs: object,
    ) -> tuple[dict, dict]:
        captured = {
            "commands": [],
            "artifact_types": [],
            "planner_requests": [],
            "reviewer_requests": [],
        }
        capacity_failures = {
            str(key): int(value)
            for key, value in dict(capacity_failures_by_artifact or {}).items()
        }
        non_capacity_failures = {
            str(key): int(value)
            for key, value in dict(non_capacity_failures_by_artifact or {}).items()
        }

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
            artifact_key = str(artifact_type or "semantic_planner_raw_request")
            captured["artifact_types"].append(artifact_key)
            if artifact_type not in {
                "semantic_oracle_raw_request",
                "semantic_reviewer_raw_request",
            }:
                captured["request"] = dict(request)
                captured["planner_requests"].append(dict(request))
            elif artifact_type == "semantic_reviewer_raw_request":
                captured["reviewer_requests"].append(dict(request))
            if capacity_failures.get(artifact_key, 0) > 0:
                capacity_failures[artifact_key] -= 1
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout="",
                    stderr="Selected model is at capacity. Please try a different model.",
                )
            if non_capacity_failures.get(artifact_key, 0) > 0:
                non_capacity_failures[artifact_key] -= 1
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout="",
                    stderr=(
                        "adapter command failed for a non-transient reason; "
                        "try a different model configuration"
                    ),
                )
            if artifact_type == "semantic_oracle_raw_request":
                oracle_kwargs = dict(adapter_kwargs)
                oracle_question_scope = oracle_kwargs.pop("oracle_question_scope", None)
                if oracle_question_scope is not None:
                    oracle_kwargs["question_scope"] = str(oracle_question_scope)
                response = self.oracle_adapter_response(request, **oracle_kwargs)
            elif artifact_type == "semantic_reviewer_raw_request":
                response = self.reviewer_adapter_response(request)
                if callable(reviewer_response_mutator):
                    response = reviewer_response_mutator(response, request)
            else:
                planner_kwargs = dict(adapter_kwargs)
                planner_kwargs.pop("oracle_question_scope", None)
                response = self.codex_adapter_response(request, **planner_kwargs)
            if artifact_type == "semantic_planner_raw_request" and callable(
                planner_response_mutator
            ):
                response = planner_response_mutator(response, request)
            elif artifact_type == "semantic_planner_raw_request" and callable(response_mutator):
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
            env[CODEX_SEMANTIC_ADAPTER_CAPACITY_RETRY_BACKOFF_SECONDS_ENV] = "0"
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
        request["_artifact_types"] = list(captured["artifact_types"])
        request["_planner_requests"] = list(captured["planner_requests"])
        request["_reviewer_requests"] = list(captured["reviewer_requests"])
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

    def reduce_candidate_to_scope(
        self,
        candidate: dict,
        *,
        angle_count: int,
        tasks_per_angle: int,
        question_scope: str = "broad",
        coverage_complete: bool = True,
    ) -> dict:
        candidate = json.loads(json.dumps(candidate))
        kept_angles = candidate["angles"][:angle_count]
        kept_angle_ids = [angle["angle_id"] for angle in kept_angles]
        kept_tasks = []
        for angle_id in kept_angle_ids:
            kept_tasks.extend(
                [
                    task
                    for task in candidate["bounded_tasks"]
                    if task["angle_id"] == angle_id
                ][:tasks_per_angle]
            )
        kept_task_ids = [task["task_id"] for task in kept_tasks]
        candidate["question_scope"] = question_scope
        candidate["angles"] = kept_angles
        candidate["bounded_tasks"] = kept_tasks
        for requirement in candidate["requirement_coverage_map"]:
            requirement["covered_by_angle_ids"] = list(kept_angle_ids)
            requirement["covered_by_task_ids"] = list(kept_task_ids)
            requirement["coverage_status"] = "covered" if coverage_complete else "not_covered"
        return candidate

    def sanitize_city_planning_candidate_for_text_only(
        self, candidate: dict, *, question: str
    ) -> dict:
        angle_specs = (
            (
                "primary_source",
                "Adopted Plan Metrics",
                "Which adopted plans define measurable implementation indicators?",
                "adopted plan metric inventory",
            ),
            (
                "official_source",
                "Agency Responsibility Mapping",
                "Which departments or partner agencies are assigned delivery responsibility?",
                "agency responsibility matrix",
            ),
            (
                "recent_change",
                "Update Cycle And Amendments",
                "What recent amendments or annual reports changed the implementation baseline?",
                "recent plan update timeline",
            ),
            (
                "comparative_analysis",
                "Cross-Jurisdiction Differences",
                "How do indicator definitions and agency roles differ across jurisdictions?",
                "cross-jurisdiction comparison table",
            ),
            (
                "counter_evidence",
                "Gaps And Conflicting Duties",
                "Where do official records omit owners or assign overlapping responsibilities?",
                "implementation gap register",
            ),
        )
        for index, angle in enumerate(candidate["angles"], start=1):
            need, title, research_question, artifact = angle_specs[
                (index - 1) % len(angle_specs)
            ]
            angle["route"] = "text_only"
            angle["evidence_need"] = need
            angle["title"] = title
            angle["research_question"] = f"{research_question} Scope: {question}."
            angle["expected_source_types"] = ["official local government plans"]
            angle["expected_visual_targets"] = []
            angle["expected_artifacts"] = [artifact, "official source notes"]
            angle["search_queries"] = [
                f"{question} {need} official local government plan"
            ]
            angle["success_criteria"] = [
                "Use official local government planning records.",
                "Record indicators, agencies, caveats, and unknowns.",
            ]
            angle["report_section"] = f"Planning Indicators {index}"
        for index, task in enumerate(candidate["bounded_tasks"], start=1):
            task["route"] = "text_only"
            task["query"] = f"{question} official planning records task {index}"
            task["expected_visual_targets"] = []
            task["expected_artifacts"] = [
                "planning indicator source notes",
                "responsible agency comparison table",
            ]
            task["success_criteria"] = [
                "Use official local government planning records.",
                "Record indicators, agencies, caveats, and unknowns.",
            ]
            task["max_images"] = 0
            task.pop("expected_evidence", None)
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
                "budget_cap",
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

    def test_semantic_substitute_check_allows_physical_architecture_domain(self) -> None:
        oracle = self.semantic_internal_leakage_oracle()
        oracle["forbidden_internal_implementation_terms"].append("architecture")
        oracle["forbidden_angles"].append("software architecture")
        plan = self.semantic_internal_leakage_review_plan(
            query=(
                "Research hospital physical architecture and facility layout, "
                "not software architecture, using public hospital design standards."
            ),
            expected_artifacts=[
                "hospital physical architecture comparison table",
                "facility layout evidence notes",
            ],
            success_criteria=[
                (
                    "Distinguish physical facility architecture, ward layout, "
                    "and patient-flow evidence."
                ),
                "Exclude software architecture material from the hospital facility review.",
            ],
            done_condition=(
                "Stop when hospital facility architecture evidence is source-backed "
                "and no software-architecture content remains."
            ),
        )

        substitute = _semantic_substitute_implementation_check(plan=plan, oracle=oracle)

        self.assertFalse(_has_forbidden_internal_leakage(plan=plan, oracle=oracle))
        self.assertTrue(substitute["passed"], substitute)
        self.assertNotIn(
            "architecture",
            substitute["forbidden_internal_implementation_terms_found"],
        )
        self.assertNotIn(
            "software architecture",
            substitute["forbidden_angle_terms_found"],
        )

    def test_semantic_substitute_check_still_blocks_software_architecture_leakage(self) -> None:
        oracle = self.semantic_internal_leakage_oracle()
        oracle["forbidden_internal_implementation_terms"].append("architecture")
        plan = self.semantic_internal_leakage_review_plan(
            query="Compare software architecture for API runtime modules.",
            expected_artifacts=["software architecture notes"],
            success_criteria=[
                "Document software architecture boundaries for runtime modules."
            ],
        )

        substitute = _semantic_substitute_implementation_check(plan=plan, oracle=oracle)

        self.assertTrue(_has_forbidden_internal_leakage(plan=plan, oracle=oracle))
        self.assertFalse(substitute["passed"], substitute)
        self.assertIn(
            "architecture",
            substitute["forbidden_internal_implementation_terms_found"],
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
            (
                "budget_cap leakage",
                "Document budget_cap propagation through review-visible plan constraints.",
                {"budget_cap"},
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
        question = "Compare OAuth device-flow provider behavior across official implementation documentation."
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
        question = "Compare OAuth device-flow provider behavior across official implementation documentation."
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
        question = "Compare OAuth device-flow provider behavior across official implementation documentation."
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
        question = "Compare OAuth device-flow provider behavior across official implementation documentation."

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
                "Compare OAuth device-flow provider behavior across official implementation documentation."
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
        question = "Compare OAuth device-flow provider behavior across official implementation documentation."
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
            "Compare OAuth device-flow provider behavior across official implementation "
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

    def test_visual_preferences_reject_all_text_candidate_before_review(self) -> None:
        question = "Compare OAuth device-flow provider behavior across official implementation documentation."

        for preference in ("visual_required", "visual_optional"):
            with self.subTest(preference=preference):
                candidate = self.text_only_oauth_candidate(question=question)
                validation = validate_semantic_candidate_plan(
                    original_question=question,
                    plan=candidate,
                    visual_preference=preference,
                )

                self.assertFalse(validation["ok"], validation)
                failure_codes = {failure["code"] for failure in validation["failures"]}
                self.assertIn("visual_requirement_missing_visual_route", failure_codes)
                self.assertIn("visual_question_all_text_only", failure_codes)

    def test_prepare_visual_optional_blocks_adapter_that_drops_visual_routes(self) -> None:
        question = "Compare OAuth device-flow provider behavior across official implementation documentation."

        with mock.patch.dict(
            "os.environ",
            {CODEX_SEMANTIC_PLANNER_VALIDATION_MAX_ATTEMPTS_ENV: "1"},
        ):
            result, adapter_request = self.prepare_with_codex_adapter(
                question,
                route="visual_optional",
                requirement_types=("subject", "source_quality"),
                visual_angle_indexes=(),
            )

        self.assert_invalid_adapter_response_blocked(
            result,
            expected_failure_codes={
                "visual_requirement_missing_visual_route",
                "visual_question_all_text_only",
            },
        )
        self.assertEqual(adapter_request["visual_preference"], "visual_optional")
        run_dir = Path(result["run_dir"])
        self.assertFalse(
            (run_dir / "semantic_reviewer_raw" / "reviewer_request.json").exists()
        )

    def test_visual_optional_rejects_visual_dominance_for_nonvisual_prompt(self) -> None:
        question = (
            "Compare architectural model outputs against public design criteria "
            "and tender document criteria."
        )
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "v" * 64,
        }
        response = self.codex_adapter_response(
            request,
            question_scope="broad",
            angle_count=5,
            tasks_per_angle=4,
            requirement_types=("subject", "source_quality"),
            visual_angle_indexes=(2, 3),
        )

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=response["candidate_plan"],
            visual_preference="visual_optional",
        )

        self.assertFalse(validation["ok"], validation)
        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertIn("MODALITY_OPTIONALITY_REVERSED", failure_codes)
        self.assertIn(
            "visual_optional_visual_work_dominates_primary_evidence",
            failure_codes,
        )
        profile = validation["visual_optional_support_profile"]
        self.assertEqual(profile["visual_angle_count"], 2)
        self.assertEqual(profile["visual_task_count"], 8)
        self.assertEqual(profile["max_visual_angles"], 1)
        self.assertEqual(profile["max_visual_tasks"], 5)

    def test_visual_optional_accepts_bounded_support_without_dropping_visual(self) -> None:
        question = (
            "Compare architectural model outputs against public design criteria "
            "and tender document criteria."
        )
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "w" * 64,
        }
        response = self.codex_adapter_response(
            request,
            question_scope="broad",
            angle_count=5,
            tasks_per_angle=4,
            requirement_types=("subject", "source_quality"),
            visual_angle_indexes=(2,),
        )
        candidate = json.loads(json.dumps(response["candidate_plan"]))
        for angle in candidate["angles"]:
            if angle["angle_id"] == "angle_002":
                angle["route"] = "visual_optional"
                angle["evidence_need"] = "visual_example"
                angle["expected_visual_targets"] = [
                    "public design document model-output figure, if available"
                ]
                continue
            angle["route"] = "text_only"
            angle["expected_visual_targets"] = []
            if angle["evidence_need"] in {"visual_example", "visual_observation"}:
                angle["evidence_need"] = "official_source"
            angle["expected_artifacts"] = [
                f"{angle['angle_id']} text document criteria notes"
            ]
            angle["success_criteria"] = [
                f"Use official text or document sources for {question}."
            ]
        for task in candidate["bounded_tasks"]:
            if task["angle_id"] == "angle_002":
                task["route"] = "visual_optional"
                task["expected_visual_targets"] = [
                    "public design document model-output figure, if available"
                ]
                task["max_images"] = 1
                task["expected_evidence"] = (
                    ["visual_observation"]
                    if task["task_id"].endswith(("006", "008"))
                    else ["visual_example"]
                )
                continue
            task["route"] = "text_only"
            task["expected_visual_targets"] = []
            task["max_images"] = 0
            task.pop("expected_evidence", None)
            task["query"] = (
                f"{question} official text document criteria task {task['task_id']}"
            )
            task["expected_artifacts"] = [
                f"{task['task_id']} source criteria notes"
            ]
            task["success_criteria"] = [
                f"Task must preserve subject: {question}.",
                "Use official text or document sources as evidence.",
            ]
            task["done_condition"] = "Stop when text or document evidence is recorded."

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
            visual_preference="visual_optional",
        )

        self.assertTrue(validation["ok"], validation)
        profile = validation["visual_optional_support_profile"]
        self.assertEqual(profile["visual_angle_count"], 1)
        self.assertEqual(profile["visual_task_count"], 4)

        all_text = json.loads(json.dumps(candidate))
        for angle in all_text["angles"]:
            angle["route"] = "text_only"
            angle["expected_visual_targets"] = []
            if angle["evidence_need"] in {"visual_example", "visual_observation"}:
                angle["evidence_need"] = "official_source"
            angle["expected_artifacts"] = [
                f"{angle['angle_id']} text document criteria notes"
            ]
            angle["success_criteria"] = [
                f"Use official text or document sources for {question}."
            ]
        for task in all_text["bounded_tasks"]:
            task["route"] = "text_only"
            task["expected_visual_targets"] = []
            task["max_images"] = 0
            task.pop("expected_evidence", None)
            task["query"] = (
                f"{question} official text document criteria task {task['task_id']}"
            )
            task["expected_artifacts"] = [
                f"{task['task_id']} source criteria notes"
            ]
            task["success_criteria"] = [
                f"Task must preserve subject: {question}.",
                "Use official text or document sources as evidence.",
            ]
            task["done_condition"] = "Stop when text or document evidence is recorded."

        all_text_validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=all_text,
            visual_preference="visual_optional",
        )

        self.assertFalse(all_text_validation["ok"], all_text_validation)
        all_text_codes = {
            failure["code"] for failure in all_text_validation["failures"]
        }
        self.assertIn("visual_requirement_missing_visual_route", all_text_codes)
        self.assertIn("visual_question_all_text_only", all_text_codes)

    def test_prepare_visual_optional_retries_visual_dominance_before_review(self) -> None:
        question = (
            "Compare architectural model outputs against public design criteria "
            "and tender document criteria."
        )

        def bounded_visual_support(response: dict) -> dict:
            response = json.loads(json.dumps(response))
            candidate = response["candidate_plan"]
            for angle in candidate["angles"]:
                if angle["angle_id"] == "angle_002":
                    angle["route"] = "visual_optional"
                    angle["evidence_need"] = "visual_example"
                    angle["expected_visual_targets"] = [
                        "public design document model-output figure, if available"
                    ]
                    continue
                angle["route"] = "text_only"
                angle["expected_visual_targets"] = []
                if angle["evidence_need"] in {"visual_example", "visual_observation"}:
                    angle["evidence_need"] = "official_source"
                angle["expected_artifacts"] = [
                    f"{angle['angle_id']} text document criteria notes"
                ]
                angle["success_criteria"] = [
                    f"Use official text or document sources for {question}."
                ]
            for task in candidate["bounded_tasks"]:
                if task["angle_id"] == "angle_002":
                    task["route"] = "visual_optional"
                    task["expected_visual_targets"] = [
                        "public design document model-output figure, if available"
                    ]
                    task["max_images"] = 1
                    task["expected_evidence"] = (
                        ["visual_observation"]
                        if task["task_id"].endswith(("006", "008"))
                        else ["visual_example"]
                    )
                    continue
                task["route"] = "text_only"
                task["expected_visual_targets"] = []
                task["max_images"] = 0
                task.pop("expected_evidence", None)
                task["query"] = (
                    f"{question} official text document criteria task {task['task_id']}"
                )
                task["expected_artifacts"] = [
                    f"{task['task_id']} source criteria notes"
                ]
                task["success_criteria"] = [
                    f"Task must preserve subject: {question}.",
                    "Use official text or document sources as evidence.",
                ]
                task["done_condition"] = "Stop when text or document evidence is recorded."
            return response

        def planner_mutator(response: dict, request: dict) -> dict:
            if request.get("retry_attempt"):
                return bounded_visual_support(response)
            return response

        result, adapter_request = self.prepare_with_codex_adapter(
            question,
            route="visual_optional",
            requirement_types=("subject", "source_quality"),
            visual_angle_indexes=(2, 3),
            planner_response_mutator=planner_mutator,
        )
        run_dir = Path(result["run_dir"])
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")[
            "semantic_plan"
        ]

        self.assertEqual(result["status"], "awaiting_search_results", result)
        self.assertEqual(adapter_request["retry_attempt"], 2)
        self.assertEqual(
            adapter_request["_artifact_types"].count("semantic_planner_raw_request"),
            2,
        )
        first_attempt_codes = set(
            raw_response["previous_adapter_attempts"][0][
                "deterministic_failure_codes"
            ]
        )
        self.assertIn("MODALITY_OPTIONALITY_REVERSED", first_attempt_codes)
        self.assertIn(
            "visual_optional_visual_work_dominates_primary_evidence",
            first_attempt_codes,
        )
        visual_angles = [
            angle
            for angle in semantic_plan["angles"]
            if angle["route"] != "text_only"
        ]
        visual_tasks = [
            task
            for task in semantic_plan["bounded_tasks"]
            if task["route"] != "text_only"
        ]
        self.assertEqual(len(visual_angles), 1)
        self.assertEqual(len(visual_tasks), 4)

    def test_req003_deliverable_remediation_and_structured_artifact_requirements(self) -> None:
        question = (
            "Compare architectural model outputs against public design criteria "
            "and tender document criteria, then prioritize remediation."
        )
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "x" * 64,
        }
        response = self.codex_adapter_response(
            request,
            question_scope="broad",
            angle_count=5,
            tasks_per_angle=4,
            requirement_types=("subject", "source_quality"),
        )
        candidate = json.loads(json.dumps(response["candidate_plan"]))
        task_ids = [task["task_id"] for task in candidate["bounded_tasks"]]
        angle_ids = [angle["angle_id"] for angle in candidate["angles"]]
        for index, task in enumerate(candidate["bounded_tasks"], start=1):
            task["query"] = (
                f"Collect public design and tender criteria source evidence {index}."
            )
            task["expected_artifacts"] = ["source evidence notes"]
            task["success_criteria"] = [
                "Record official source evidence and caveats."
            ]
            task["done_condition"] = "Stop when source-backed criteria notes are recorded."
        candidate["requirement_coverage_map"].append(
            {
                "requirement_id": "req_003",
                "requirement_type": "analysis_comparison_output_shape",
                "requirement_text": (
                    "Produce a side-by-side comparison table for architectural "
                    "model outputs and structured artifacts against public design "
                    "criteria and tender document criteria, including match, "
                    "partial, mismatch, unverifiable, evidence, caveats, and "
                    "prioritized remediation recommendations."
                ),
                "prompt_text": "side-by-side comparison table",
                "prompt_span": {"start": None, "end": None},
                "explicit": True,
                "non_negotiable": True,
                "covered_by_angle_ids": angle_ids,
                "covered_by_task_ids": task_ids,
                "coverage_status": "covered",
            }
        )

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
        )

        self.assertFalse(validation["ok"], validation)
        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertIn("REQ_003_COMPARISON_DELIVERABLE_INCOMPLETE", failure_codes)
        self.assertIn("REQ_003_PRIORITIZED_REMEDIATION_MISSING", failure_codes)
        self.assertIn("STRUCTURED_ARTIFACT_ROUTE_INCOMPLETE", failure_codes)

        repaired = json.loads(json.dumps(candidate))
        task = repaired["bounded_tasks"][0]
        task["route"] = "text_only"
        task["expected_visual_targets"] = []
        task["max_images"] = 0
        task["query"] = (
            "Assess structured architectural model output artifacts by inventorying "
            "files, fields, attributes, object classes, LOD, and IFC properties, then "
            "build the side-by-side comparison table."
        )
        task["expected_artifacts"] = [
            "structured artifact assessment inventory",
            "consolidated side-by-side comparison table",
            "prioritized remediation recommendations",
        ]
        task["success_criteria"] = [
            (
                "The table has fields for requirement or criterion, architectural "
                "model output or structured artifact, public design criteria, tender "
                "document criteria, match status, partial status, mismatch status, "
                "unverifiable status, evidence citations, caveats, and remediation."
            ),
            (
                "Remediation recommendations are ranked by priority, severity, "
                "impact, effort, and evidence confidence."
            ),
        ]
        task["done_condition"] = (
            "Stop when each structured artifact requirement has assessment fields, "
            "comparison evidence, caveats, and prioritized remediation next action."
        )

        repaired_validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=repaired,
        )

        self.assertTrue(repaired_validation["ok"], repaired_validation)
        repaired_codes = {
            failure["code"] for failure in repaired_validation["failures"]
        }
        self.assertNotIn("REQ_003_COMPARISON_DELIVERABLE_INCOMPLETE", repaired_codes)
        self.assertNotIn("REQ_003_PRIORITIZED_REMEDIATION_MISSING", repaired_codes)
        self.assertNotIn("STRUCTURED_ARTIFACT_ROUTE_INCOMPLETE", repaired_codes)

    def test_req003_food_safety_investigation_does_not_require_compliance_matrix(self) -> None:
        question = "".join(
            [
                "테스트라는 표현이 식품 안전 검사 맥락에서 ",
                "쓰일 때 공식 검사 기준을 조사해줘.",
            ]
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
                "analysis_and_output_shape",
            ),
        )
        candidate = json.loads(json.dumps(response["candidate_plan"]))
        req_003 = candidate["requirement_coverage_map"][2]
        req_003.update(
            {
                "requirement_id": "req_003",
                "requirement_type": "analysis_and_output_shape",
                "requirement_text": (
                    "‘테스트’라는 포괄적 표현을 공식 용어와 대응시키고, 검사 "
                    "목적·대상·방법·시료채취·판정기준·후속조치 및 기준의 "
                    "법적 지위를 구조적으로 분석한다. 관할 또는 검사 유형에 "
                    "따라 의미가 달라지면 비교해 제시한다."
                ),
                "prompt_text": question,
                "expected_modalities": ["text"],
                "output_shape_constraints": [
                    "범위 및 용어 정의",
                    "공식 기준 체계 요약",
                    "검사 절차와 판정 요소",
                    "관할·검사유형별 차이",
                    "근거 출처",
                    "한계와 확인 필요사항",
                ],
            }
        )
        for angle in candidate["angles"]:
            angle["route"] = "text_only"
            angle["evidence_need"] = "official_source"
            angle["expected_visual_targets"] = []
            angle["expected_artifacts"] = [
                "food safety inspection terminology notes",
                "official inspection criteria summary",
            ]
            angle["success_criteria"] = [
                "Explain official food-safety inspection terminology and criteria.",
                "Separate jurisdiction-specific differences from general definitions.",
            ]
        for task in candidate["bounded_tasks"]:
            task["route"] = "text_only"
            task["expected_visual_targets"] = []
            task["max_images"] = 0
            task["query"] = (
                f"{question} 공식 식품 안전 검사 용어 기준 판정기준 후속조치 "
                f"source-backed task {task['task_id']}"
            )
            task["expected_artifacts"] = [
                "food safety inspection terminology notes",
                "official inspection criteria summary",
            ]
            task["success_criteria"] = [
                "Use official, regulatory, or primary sources for support.",
                "Explain terminology, test method, sampling, decision criteria, and caveats.",
            ]
            task["done_condition"] = (
                "Stop when official food-safety inspection terminology, criteria, "
                "jurisdiction limits, evidence, and caveats are recorded."
            )

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
            visual_preference="text_only",
        )

        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertNotIn("REQ_003_COMPARISON_DELIVERABLE_INCOMPLETE", failure_codes)
        self.assertNotIn("REQ_003_PRIORITIZED_REMEDIATION_MISSING", failure_codes)

    def test_visual_difference_table_does_not_require_prioritized_remediation(self) -> None:
        question = "".join(
            [
                "한국 지하철 안전 픽토그램 이미지와 ",
                "공식 행동요령의 차이를 분석해줘.",
            ]
        )
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "p" * 64,
        }
        response = self.codex_adapter_response(
            request,
            question_scope="broad",
            angle_count=5,
            tasks_per_angle=4,
            requirement_types=("subject", "source_quality", "visual_modality"),
            visual_angle_indexes=(2, 3),
        )
        candidate = json.loads(json.dumps(response["candidate_plan"]))
        req_003 = candidate["requirement_coverage_map"][2]
        req_003.update(
            {
                "requirement_id": "req_003",
                "requirement_type": "requested analysis/comparison/output shape",
                "requirement_text": (
                    "안전 상황별로 픽토그램의 의미를 해석하고 공식 행동요령과 "
                    "대조하여 일치점, 누락, 단순화, 모호성, 순서·조건·예외의 "
                    "차이 및 잠재적 안전 영향을 구조적으로 설명한다."
                ),
                "prompt_text": question,
                "expected_modalities": ["visual interpretation", "textual comparison"],
                "output_shape_constraints": [
                    "상황별 비교표",
                    "이미지 또는 이미지 링크·식별 정보",
                    "핵심 차이 요약",
                    "안전상 의미 또는 개선점",
                    "출처 인용",
                ],
            }
        )
        comparison_task = candidate["bounded_tasks"][0]
        comparison_task["route"] = "text_only"
        comparison_task["max_images"] = 0
        comparison_task["expected_visual_targets"] = []
        comparison_task["query"] = (
            "한국 지하철 안전 픽토그램 시각 메시지와 공식 행동요령 차이 "
            "상황별 비교표 근거 출처"
        )
        comparison_task["expected_artifacts"] = [
            "상황별 비교표",
            "픽토그램 시각 메시지와 공식 행동요령 차이 근거",
        ]
        comparison_task["success_criteria"] = [
            "Compare each pictogram visual message with the official behavior guidance.",
            "Record differences, omissions, ambiguity, safety meaning, and source evidence.",
        ]
        comparison_task["done_condition"] = (
            "Stop when the situation-by-situation comparison table has source-backed "
            "pictogram meanings, official guidance, differences, and caveats."
        )

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
            visual_preference="visual_required",
        )

        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertNotIn("REQ_003_COMPARISON_DELIVERABLE_INCOMPLETE", failure_codes)
        self.assertNotIn("REQ_003_PRIORITIZED_REMEDIATION_MISSING", failure_codes)

    def test_req003_reviewer_retry_materializes_text_only_comparison_schema(self) -> None:
        question = (
            "Compare OAuth device-flow provider documentation against official "
            "implementation requirements."
        )
        candidate = self.text_only_oauth_candidate(question=question)
        task_ids = [task["task_id"] for task in candidate["bounded_tasks"]]
        angle_ids = [angle["angle_id"] for angle in candidate["angles"]]
        candidate["requirement_coverage_map"].append(
            {
                "requirement_id": "req_003",
                "requirement_type": "analysis_comparison_output_shape",
                "requirement_text": (
                    "Produce a side-by-side comparison table of provider "
                    "documentation against official implementation requirements."
                ),
                "prompt_text": "side-by-side comparison table",
                "prompt_span": {"start": None, "end": None},
                "explicit": True,
                "non_negotiable": True,
                "covered_by_angle_ids": angle_ids,
                "covered_by_task_ids": task_ids,
                "coverage_status": "covered",
            }
        )
        candidate["angles"][4]["evidence_need"] = "comparative_analysis"
        candidate["angles"][4]["title"] = "OAuth Provider Requirement Comparison Synthesis"
        candidate["angles"][4]["research_question"] = (
            "How should OAuth provider documentation rows be compared against "
            "official implementation requirements?"
        )
        for task in candidate["bounded_tasks"][:4]:
            task["query"] = (
                "Collect official OAuth device-flow source evidence for the "
                f"source-baseline angle: {task['task_id']}"
            )
            task["expected_artifacts"] = ["official source evidence notes"]
            task["success_criteria"] = [
                "Stay scoped to official-source collection for angle_001."
            ]
            task["done_condition"] = (
                "Stop when official source evidence for angle_001 is recorded."
            )
        comparison_task = candidate["bounded_tasks"][18]
        comparison_task["angle_id"] = candidate["angles"][4]["angle_id"]
        comparison_task["query"] = (
            "Build OAuth provider documentation rows against official "
            "implementation requirements."
        )
        comparison_task["expected_artifacts"] = [
            "side-by-side provider requirement comparison table"
        ]
        comparison_task["success_criteria"] = [
            "Compare provider documentation against official requirements with evidence."
        ]
        comparison_task["done_condition"] = (
            "Stop when source-backed comparison rows are ready."
        )

        repaired, materializations = _materialize_candidate_req003_comparison_deliverable(
            candidate,
            raw_request={
                "visual_preference": "text_only",
                "semantic_convergence_attempt": 2,
                "previous_semantic_review_failure_codes": [
                    "REQ_003_COMPARISON_DELIVERABLE_INCOMPLETE"
                ],
            },
            original_question=question,
        )

        self.assertTrue(materializations, materializations)
        self.assertEqual(
            materializations[0]["task_id"],
            comparison_task["task_id"],
        )
        self.assertEqual(
            materializations[0]["materialization"],
            "materialized_req003_comparison_deliverable",
        )
        self.assertNotIn(
            "comparison row schema",
            json.dumps(repaired["bounded_tasks"][0]).lower(),
        )
        repaired_task = repaired["bounded_tasks"][18]
        self.assertEqual(repaired_task["route"], "text_only")
        self.assertEqual(repaired_task["expected_visual_targets"], [])
        self.assertEqual(repaired_task["max_images"], 0)
        self.assertIn("Build OAuth provider documentation rows", repaired_task["query"])
        self.assertNotIn(question, repaired_task["query"])
        repaired_task_text = json.dumps(repaired_task, ensure_ascii=False).lower()
        for expected in ("status", "evidence", "caveat", "remediation"):
            self.assertIn(expected, repaired_task_text)

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=repaired,
            visual_preference="text_only",
        )
        self.assertTrue(validation["ok"], validation)

    def test_req003_reviewer_retry_appends_to_comparison_angle_when_no_task_matches(self) -> None:
        question = (
            "Compare OAuth provider notices against official implementation requirements."
        )
        candidate = self.text_only_oauth_candidate(question=question)
        candidate["bounded_tasks"] = candidate["bounded_tasks"][:19]
        candidate["angles"][4]["evidence_need"] = "comparative_analysis"
        candidate["angles"][4]["title"] = "OAuth Notice Requirement Comparison"
        candidate["angles"][4]["research_question"] = (
            "Which final comparison rows reconcile OAuth notices with official requirements?"
        )
        for task in candidate["bounded_tasks"]:
            task["query"] = (
                "Collect bounded official OAuth source evidence for "
                f"{task['angle_id']} {task['task_id']}."
            )
            task["expected_artifacts"] = ["official source notes"]
            task["success_criteria"] = ["Keep this task scoped to source collection."]
            task["done_condition"] = "Stop when source evidence notes are recorded."
        candidate["requirement_coverage_map"].append(
            {
                "requirement_id": "req_003",
                "requirement_type": "analysis_comparison_output_shape",
                "requirement_text": (
                    "Produce a side-by-side comparison deliverable with status, "
                    "evidence, caveat, and remediation fields."
                ),
                "prompt_text": "comparison deliverable",
                "prompt_span": {"start": None, "end": None},
                "explicit": True,
                "non_negotiable": True,
                "covered_by_angle_ids": [candidate["angles"][4]["angle_id"]],
                "covered_by_task_ids": [],
                "coverage_status": "covered",
            }
        )

        repaired, materializations = _materialize_candidate_req003_comparison_deliverable(
            candidate,
            raw_request={
                "visual_preference": "text_only",
                "semantic_convergence_attempt": 2,
                "previous_semantic_review_failure_codes": [
                    "REQ_003_COMPARISON_DELIVERABLE_INCOMPLETE"
                ],
            },
            original_question=question,
        )

        self.assertEqual(len(repaired["bounded_tasks"]), 20)
        self.assertEqual(materializations[0]["task_action"], "appended")
        appended_task = repaired["bounded_tasks"][-1]
        self.assertEqual(appended_task["angle_id"], candidate["angles"][4]["angle_id"])
        self.assertEqual(appended_task["route"], "text_only")
        self.assertEqual(appended_task["expected_visual_targets"], [])
        self.assertEqual(appended_task["max_images"], 0)
        self.assertNotIn(question, appended_task["query"])
        appended_text = json.dumps(appended_task, ensure_ascii=False).lower()
        for expected in ("status", "evidence", "caveat", "remediation"):
            self.assertIn(expected, appended_text)

    def test_generic_analysis_comparison_type_does_not_force_comparison_table(self) -> None:
        question = (
            "Research testing requirements for drinking-water lead sampling, "
            "distinguishing lab testing from school IT systems."
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
            requirement_types=(
                "subject",
                "source_quality",
                "requested analysis/comparison/output shape",
            ),
        )
        candidate = json.loads(json.dumps(response["candidate_plan"]))
        req_003 = candidate["requirement_coverage_map"][2]
        req_003.update(
            {
                "requirement_id": "req_003",
                "requirement_type": "requested analysis/comparison/output shape",
                "requirement_text": (
                    "Explicitly separate requirements for physical sampling and "
                    "laboratory analysis from requirements or capabilities of school "
                    "information-technology systems, and explain the boundary between them."
                ),
                "prompt_text": "distinguishing lab testing from school IT systems",
                "expected_modalities": ["comparative textual analysis"],
                "output_shape_constraints": [
                    "Separate sections for sampling/laboratory testing and school IT systems",
                    "Direct comparison of responsibilities, functions, and non-overlap",
                    "Concise synthesis of where data transfer or recordkeeping connects the two domains",
                ],
            }
        )
        for index, angle in enumerate(candidate["angles"], start=1):
            angle["route"] = "text_only"
            angle["evidence_need"] = "official_source"
            angle["title"] = f"Drinking-water lead sampling requirement angle {index}"
            angle["research_question"] = (
                "Which official lead sampling, laboratory testing, or school IT "
                "system requirement does this angle establish?"
            )
            angle["expected_visual_targets"] = []
            angle["expected_artifacts"] = ["official requirement notes"]
            angle["success_criteria"] = [
                "Use official, regulatory, or primary text sources.",
                "Keep laboratory testing and school IT system obligations separate.",
            ]
        for task in candidate["bounded_tasks"]:
            task["route"] = "text_only"
            task["expected_visual_targets"] = []
            task["max_images"] = 0
            task["query"] = (
                "drinking-water lead sampling laboratory testing school IT systems "
                f"official requirements source-backed task {task['task_id']}"
            )
            task["expected_artifacts"] = ["official requirement notes"]
            task["success_criteria"] = [
                "Use official, regulatory, or primary text sources.",
                "Distinguish laboratory testing obligations from school IT system obligations.",
            ]
            task["done_condition"] = (
                "Stop when source-backed requirements, boundaries, caveats, and "
                "unknown jurisdiction limits are recorded."
            )

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
            visual_preference="text_only",
        )

        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertNotIn("REQ_003_COMPARISON_DELIVERABLE_INCOMPLETE", failure_codes)
        self.assertNotIn("REQ_003_PRIORITIZED_REMEDIATION_MISSING", failure_codes)

    def test_semantic_planner_source_has_no_sem_reg_004_special_case(self) -> None:
        source = (
            ROOT
            / "plugins"
            / "codex-deepresearch"
            / "src"
            / "deepresearch"
            / "semantic_planner.py"
        ).read_text(encoding="utf-8")

        for forbidden in (
            "sem" + "-reg-004",
            "sem_reg_004",
            "b9301c1",
            "dr_20260720T141947",
            "".join(
                [
                    "건축 architecture 모델 산출물을 ",
                    "공공 설계 기준과 입찰 문서 기준으로 비교해줘",
                ]
            ),
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

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

    def test_semantic_reviewer_failure_converges_with_retry_diagnostics(self) -> None:
        question = "Compare official museum visitor map images with accessibility guidance."
        repair_marker = "reviewer blocker repaired by semantic convergence"

        def planner_mutator(response: dict, request: dict) -> dict:
            candidate = response["candidate_plan"]
            if request.get("semantic_convergence_attempt"):
                candidate["constraints"].append(repair_marker)
                candidate["bounded_tasks"][0]["done_condition"] = (
                    "Stop when the reviewer blocker repair marker is present and the "
                    "visual comparison task is bounded."
                )
            return response

        def reviewer_mutator(response: dict, request: dict) -> dict:
            plan_text = json.dumps(request["semantic_plan"], ensure_ascii=False).lower()
            if repair_marker not in plan_text:
                review = response["semantic_plan_review"]
                review["semantic_fit_score"] = 8.4
                review["blockers"] = [
                    {
                        "code": "NON_EXECUTABLE_TASK_SCOPE_CAP",
                        "message": "Split or bound the visual comparison before release.",
                    }
                ]
                review["verdict"] = "release_ineligible"
            return response

        result, adapter_request = self.prepare_with_codex_adapter(
            question,
            requirement_types=("subject", "visual_modality", "source_quality"),
            visual_angle_indexes=(2, 3),
            planner_response_mutator=planner_mutator,
            reviewer_response_mutator=reviewer_mutator,
        )
        run_dir = Path(result["run_dir"])
        convergence = self.load_json(run_dir / SEMANTIC_PLANNER_CONVERGENCE_FILENAME)
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")["semantic_plan"]

        self.assertEqual(result["status"], "awaiting_search_results")
        self.assertEqual(convergence["status"], "converged")
        self.assertEqual(convergence["attempt_count"], 2)
        self.assertEqual(len(adapter_request["_planner_requests"]), 2)
        self.assertEqual(len(adapter_request["_reviewer_requests"]), 2)
        first_attempt = convergence["attempts"][0]
        first_reviewer_hash = adapter_request["_reviewer_requests"][0][
            "semantic_plan_candidate_hash"
        ]
        self.assertEqual(first_attempt["candidate_hash"], first_reviewer_hash)
        self.assertIn("reviewed_plan_candidate_hash", first_attempt)
        self.assertNotEqual(
            first_attempt["reviewed_plan_candidate_hash"],
            first_reviewer_hash,
        )
        self.assertIn(
            "NON_EXECUTABLE_TASK_SCOPE_CAP",
            first_attempt["reviewer_blocker_codes"],
        )
        self.assertIn(
            "NON_EXECUTABLE_TASK_SCOPE_CAP",
            first_attempt["repair_inputs"]["reviewer_failure_codes"],
        )
        retry_request = adapter_request["_planner_requests"][1]
        self.assertIn(
            "NON_EXECUTABLE_TASK_SCOPE_CAP",
            retry_request["previous_semantic_review_failure_codes"],
        )
        self.assertIn("semantic_convergence_repair_inputs", retry_request)
        self.assertTrue(convergence["attempts"][1]["final_selection"])
        self.assertIn(repair_marker, json.dumps(semantic_plan, ensure_ascii=False))
        self.assertIn("semantic_planner_convergence", result["artifacts"])

    def test_semantic_reviewer_specific_blockers_add_actionable_retry_guidance(self) -> None:
        question = (
            "Compare architectural model outputs against public design criteria "
            "and tender document criteria."
        )
        repair_marker = "specific reviewer blockers repaired"

        def bounded_visual_support(response: dict) -> dict:
            response = json.loads(json.dumps(response))
            candidate = response["candidate_plan"]
            for angle in candidate["angles"]:
                if angle["angle_id"] == "angle_002":
                    angle["route"] = "visual_optional"
                    angle["evidence_need"] = "visual_example"
                    angle["expected_visual_targets"] = [
                        "public design document model-output figure, if available"
                    ]
                    continue
                angle["route"] = "text_only"
                angle["expected_visual_targets"] = []
                if angle["evidence_need"] in {"visual_example", "visual_observation"}:
                    angle["evidence_need"] = "official_source"
                angle["expected_artifacts"] = [
                    f"{angle['angle_id']} text document criteria notes"
                ]
                angle["success_criteria"] = [
                    f"Use official text or document sources for {question}."
                ]
            for task in candidate["bounded_tasks"]:
                if task["angle_id"] == "angle_002":
                    task["route"] = "visual_optional"
                    task["expected_visual_targets"] = [
                        "public design document model-output figure, if available"
                    ]
                    task["max_images"] = 1
                    task["expected_evidence"] = (
                        ["visual_observation"]
                        if task["task_id"].endswith(("006", "008"))
                        else ["visual_example"]
                    )
                    continue
                task["route"] = "text_only"
                task["expected_visual_targets"] = []
                task["max_images"] = 0
                task.pop("expected_evidence", None)
                task["query"] = (
                    f"{question} official text document criteria task {task['task_id']}"
                )
                task["expected_artifacts"] = [
                    f"{task['task_id']} source criteria notes"
                ]
                task["success_criteria"] = [
                    f"Task must preserve subject: {question}.",
                    "Use official text or document sources as evidence.",
                ]
                task["done_condition"] = "Stop when text or document evidence is recorded."
            return response

        def planner_mutator(response: dict, request: dict) -> dict:
            response = bounded_visual_support(response)
            if request.get("semantic_convergence_attempt"):
                instructions = request["planner_retry_instructions"]
                for expected in (
                    "route=visual_optional rather than route=visual_required",
                    "consolidated side-by-side comparison deliverable",
                    "match/partial/mismatch/unverifiable",
                    "prioritized remediation recommendations",
                    "structured-artifact assessment tasks",
                ):
                    self.assertIn(expected, instructions)
                candidate = response["candidate_plan"]
                candidate["constraints"].append(repair_marker)
                task = candidate["bounded_tasks"][0]
                task["expected_artifacts"] = [
                    "consolidated side-by-side comparison deliverable",
                    "prioritized remediation recommendations",
                    "structured-artifact assessment inventory",
                ]
                task["success_criteria"] = [
                    (
                        "Include criterion, architectural model output, public design "
                        "criteria, tender document criteria, match, partial, mismatch, "
                        "unverifiable, evidence, caveats, and remediation fields."
                    ),
                    (
                        "Rank prioritized remediation recommendations by severity, "
                        "impact, effort, and evidence confidence."
                    ),
                ]
                task["done_condition"] = (
                    "Stop when structured-artifact assessment tasks feed the "
                    "comparison deliverable and remediation priorities."
                )
            return response

        def reviewer_mutator(response: dict, request: dict) -> dict:
            plan_text = json.dumps(request["semantic_plan"], ensure_ascii=False)
            if repair_marker not in plan_text:
                review = response["semantic_plan_review"]
                review["semantic_fit_score"] = 7.4
                review["blockers"] = [
                    {"code": "MODALITY_OPTIONALITY_REVERSED"},
                    {"code": "REQ_003_COMPARISON_DELIVERABLE_INCOMPLETE"},
                    {"code": "REQ_003_PRIORITIZED_REMEDIATION_MISSING"},
                    {"code": "STRUCTURED_ARTIFACT_ROUTE_INCOMPLETE"},
                ]
                review["verdict"] = "release_ineligible"
            return response

        result, adapter_request = self.prepare_with_codex_adapter(
            question,
            route="visual_optional",
            requirement_types=("subject", "source_quality"),
            visual_angle_indexes=(2,),
            planner_response_mutator=planner_mutator,
            reviewer_response_mutator=reviewer_mutator,
        )
        run_dir = Path(result["run_dir"])
        convergence = self.load_json(run_dir / SEMANTIC_PLANNER_CONVERGENCE_FILENAME)
        retry_request = adapter_request["_planner_requests"][1]

        self.assertEqual(result["status"], "awaiting_search_results", result)
        self.assertEqual(convergence["status"], "converged", convergence)
        self.assertEqual(convergence["attempt_count"], 2)
        self.assertIn(
            "MODALITY_OPTIONALITY_REVERSED",
            retry_request["previous_semantic_review_failure_codes"],
        )
        self.assertIn(
            "REQ_003_COMPARISON_DELIVERABLE_INCOMPLETE",
            retry_request["planner_retry_instructions"],
        )
        self.assertIn(
            "STRUCTURED_ARTIFACT_ROUTE_INCOMPLETE",
            retry_request["planner_retry_instructions"],
        )

    def test_korean_cross_modal_req003_reviewer_retry_materializes_schema(self) -> None:
        question = "학교 급식 알레르기 표시 사진과 교육청 공식 안내 기준을 대조해줘."

        def planner_mutator(response: dict, _request: dict) -> dict:
            response = json.loads(json.dumps(response))
            candidate = response["candidate_plan"]
            req_003 = candidate["requirement_coverage_map"][2]
            req_003.update(
                {
                    "requirement_id": "req_003",
                    "requirement_type": "analysis_comparison_output_shape",
                    "requirement_text": (
                        "Compare school meal allergy label examples against "
                        "official display regulations in a side-by-side table."
                    ),
                    "prompt_text": "비교",
                    "expected_modalities": [
                        "visual interpretation",
                        "textual comparison",
                    ],
                    "output_shape_constraints": [
                        "side-by-side comparison table",
                        "source-backed evidence",
                    ],
                    "non_negotiable": True,
                    "coverage_status": "covered",
                }
            )
            candidate["angles"][0]["evidence_need"] = "official_primary_documents"
            candidate["angles"][0]["title"] = "한국 학교 급식 알레르기 공식 표시 규정의 법적 기준"
            candidate["angles"][4]["route"] = "text_only"
            candidate["angles"][4]["evidence_need"] = "structured_compliance_comparison"
            candidate["angles"][4]["expected_visual_targets"] = []
            candidate["angles"][4]["title"] = (
                "한국 학교 급식 알레르기 라벨과 공식 표시 규정 대조"
            )
            for task in candidate["bounded_tasks"][:4]:
                task["route"] = "text_only"
                task["expected_visual_targets"] = []
                task["max_images"] = 0
                task["query"] = (
                    "한국 학교 급식 알레르기 공식 표시 규정 법적 기준 "
                    f"출처 수집 {task['task_id']}"
                )
                task["expected_artifacts"] = ["공식 표시 규정 출처 노트"]
                task["success_criteria"] = [
                    "angle_001 공식 규정 출처 수집 범위를 유지한다."
                ]
                task["done_condition"] = "공식 규정 출처 근거가 기록되면 중지한다."
            comparison_task = candidate["bounded_tasks"][18]
            comparison_task["angle_id"] = candidate["angles"][4]["angle_id"]
            comparison_task["route"] = "text_only"
            comparison_task["expected_visual_targets"] = []
            comparison_task["max_images"] = 0
            comparison_task["query"] = (
                "요구사항별 공식 기준과 각 이미지 관찰값을 좌우로 배치한 "
                "준수 대조표를 작성하라."
            )
            comparison_task["expected_artifacts"] = [
                "학교 급식 알레르기 표시 준수 대조표"
            ]
            comparison_task["success_criteria"] = [
                "공식 규정 기준과 이미지 관찰값의 차이를 근거와 함께 비교한다."
            ]
            comparison_task["done_condition"] = (
                "요구사항별 대조표 행이 근거와 함께 준비되면 중지한다."
            )
            return response

        def reviewer_mutator(response: dict, request: dict) -> dict:
            plan_text = json.dumps(request["semantic_plan"], ensure_ascii=False).lower()
            required_terms = ("status", "evidence", "caveat", "remediation")
            if not all(term in plan_text for term in required_terms):
                review = response["semantic_plan_review"]
                review["semantic_fit_score"] = 8.9
                review["blockers"] = [
                    {
                        "code": "REQ_003_COMPARISON_DELIVERABLE_INCOMPLETE",
                        "message": (
                            "Req_003 comparison/output-shape oracle requirements "
                            "need a bounded side-by-side comparison deliverable "
                            "with status, evidence, caveat, and remediation fields."
                        ),
                    }
                ]
                review["verdict"] = "release_ineligible"
            return response

        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            requirement_types=(
                "subject",
                "source_quality",
                "analysis_comparison_output_shape",
            ),
            visual_angle_indexes=(2, 3),
            planner_response_mutator=planner_mutator,
            reviewer_response_mutator=reviewer_mutator,
        )
        run_dir = Path(result["run_dir"])
        convergence = self.load_json(run_dir / SEMANTIC_PLANNER_CONVERGENCE_FILENAME)
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")[
            "semantic_plan"
        ]

        self.assertEqual(result["status"], "awaiting_search_results", result)
        self.assertEqual(convergence["status"], "converged", convergence)
        self.assertEqual(convergence["attempt_count"], 2)
        self.assertIn(
            "candidate_plan_req003_comparison_deliverable_materializations",
            raw_response,
        )
        materializations = raw_response[
            "candidate_plan_req003_comparison_deliverable_materializations"
        ]
        self.assertTrue(materializations, raw_response)
        materialization = materializations[0]
        self.assertEqual(materialization["task_action"], "strengthened")
        selected_task_id = materialization["task_id"]
        self.assertNotEqual(selected_task_id, "task_semantic_001")
        self.assertTrue(
            any(task["route"] != "text_only" for task in semantic_plan["bounded_tasks"])
        )
        self.assertNotIn(
            "comparison row schema",
            json.dumps(semantic_plan["bounded_tasks"][0]).lower(),
        )
        official_angle_id = semantic_plan["angles"][0]["angle_id"]
        official_source_task_ids = {
            task["task_id"]
            for task in semantic_plan["bounded_tasks"]
            if task.get("angle_id") == official_angle_id
        }
        self.assertNotIn(selected_task_id, official_source_task_ids)
        comparison_tasks = [
            task
            for task in semantic_plan["bounded_tasks"]
            if "comparison row schema" in json.dumps(task).lower()
        ]
        self.assertTrue(comparison_tasks, semantic_plan)
        comparison_task = next(
            (
                task
                for task in comparison_tasks
                if task.get("task_id") == selected_task_id
            ),
            None,
        )
        self.assertIsNotNone(comparison_task, comparison_tasks)
        assert comparison_task is not None
        self.assertEqual(materialization["angle_id"], comparison_task["angle_id"])
        selected_angle = next(
            (
                angle
                for angle in semantic_plan["angles"]
                if angle.get("angle_id") == comparison_task["angle_id"]
            ),
            {},
        )
        self.assertIn(
            selected_angle.get("evidence_need"),
            {"structured_compliance_comparison", "comparative_analysis", "synthesis"},
        )
        self.assertEqual(comparison_task["route"], "text_only")
        self.assertEqual(comparison_task["expected_visual_targets"], [])
        self.assertEqual(comparison_task["max_images"], 0)
        self.assertNotIn(question, comparison_task["query"])
        query_prefix = comparison_task["query"].split("행별 판정 상태", 1)[0]
        self.assertTrue(
            any(
                term in query_prefix
                for term in (
                    "공식 기준",
                    "이미지 관찰값",
                    "대조표",
                    "준수",
                    "상충 판정",
                    "법적 단정",
                    "누락된 근거",
                )
            ),
            comparison_task["query"],
        )
        comparison_text = json.dumps(comparison_task, ensure_ascii=False).lower()
        for expected in ("status", "evidence", "caveat", "remediation"):
            self.assertIn(expected, comparison_text)

    def test_semantic_reviewer_failure_terminal_after_bounded_convergence(self) -> None:
        def reviewer_mutator(response: dict, _request: dict) -> dict:
            review = response["semantic_plan_review"]
            review["semantic_fit_score"] = 8.3
            review["blockers"] = [
                {
                    "code": "NON_EXECUTABLE_TASK_SCOPE_CAP",
                    "message": "Still not executable within declared caps.",
                }
            ]
            review["verdict"] = "release_ineligible"
            return response

        with mock.patch.dict(
            "os.environ",
            {CODEX_SEMANTIC_PLANNER_VALIDATION_MAX_ATTEMPTS_ENV: "2"},
        ):
            result, adapter_request = self.prepare_with_codex_adapter(
                "Compare official transit station map images with wayfinding guidance.",
                requirement_types=("subject", "visual_modality", "source_quality"),
                visual_angle_indexes=(2, 3),
                reviewer_response_mutator=reviewer_mutator,
            )
        run_dir = Path(result["run_dir"])
        convergence = self.load_json(run_dir / SEMANTIC_PLANNER_CONVERGENCE_FILENAME)

        self.assertEqual(result["status"], "blocked_semantic_review_failed")
        self.assertEqual(convergence["status"], "blocked_convergence_failed")
        self.assertEqual(convergence["attempt_count"], 2)
        self.assertEqual(len(adapter_request["_planner_requests"]), 2)
        self.assertEqual(len(adapter_request["_reviewer_requests"]), 2)
        self.assertTrue(convergence["attempts"][-1]["terminal_failure"])
        self.assertIn(
            "NON_EXECUTABLE_TASK_SCOPE_CAP",
            convergence["terminal_failure"]["reason_codes"],
        )

    def test_adapter_invalid_candidate_validation_retries_in_outer_convergence(self) -> None:
        def over_cap_until_convergence_retry(response: dict, request: dict) -> dict:
            response = json.loads(json.dumps(response))
            if request.get("semantic_convergence_attempt"):
                return response
            task = response["candidate_plan"]["bounded_tasks"][0]
            task["query"] = "Compare eight vendors using official source records."
            task["max_sources"] = 1
            task["max_images"] = 1
            task["route"] = "visual_required"
            task["expected_visual_targets"] = ["official poster images"]
            task["done_condition"] = (
                "Done when at least four images and eight vendors have official "
                "source records with caveats."
            )
            return response

        with mock.patch.dict(
            "os.environ",
            {CODEX_SEMANTIC_PLANNER_VALIDATION_MAX_ATTEMPTS_ENV: "2"},
        ):
            result, adapter_request = self.prepare_with_codex_adapter(
                "Compare official public poster image guidance across agencies.",
                requirement_types=("subject", "source_quality", "visual_modality"),
                visual_angle_indexes=(2, 3),
                planner_response_mutator=over_cap_until_convergence_retry,
            )
        run_dir = Path(result["run_dir"])
        convergence = self.load_json(run_dir / SEMANTIC_PLANNER_CONVERGENCE_FILENAME)

        self.assertEqual(result["status"], "awaiting_search_results")
        self.assertEqual(convergence["status"], "converged")
        self.assertEqual(convergence["attempt_count"], 2)
        self.assertEqual(len(adapter_request["_planner_requests"]), 3)
        first_attempt = convergence["attempts"][0]
        self.assertEqual(
            first_attempt["planner_status"],
            "blocked_semantic_planner_unavailable",
        )
        self.assertFalse(first_attempt["terminal_failure"])
        self.assertIn(
            "bounded_task_requirement_exceeds_max_sources",
            first_attempt["deterministic_failure_codes"],
        )
        self.assertIn(
            "bounded_task_requirement_exceeds_max_images",
            first_attempt["deterministic_failure_codes"],
        )
        self.assertEqual(
            first_attempt["repair_inputs"]["retry_source"],
            "adapter_invalid_response_candidate_validation",
        )
        retry_request = adapter_request["_planner_requests"][-1]
        self.assertEqual(retry_request["semantic_convergence_attempt"], 2)
        self.assertIn(
            "bounded_task_requirement_exceeds_max_sources",
            retry_request["semantic_convergence_repair_inputs"][
                "deterministic_failure_codes"
            ],
        )
        self.assertTrue(convergence["attempts"][1]["final_selection"])

    def test_adapter_invalid_candidate_validation_blocks_after_outer_max_attempts(self) -> None:
        def always_over_cap(response: dict, _request: dict) -> dict:
            response = json.loads(json.dumps(response))
            task = response["candidate_plan"]["bounded_tasks"][0]
            task["query"] = "Compare eight vendors using official source records."
            task["max_sources"] = 1
            task["max_images"] = 1
            task["route"] = "visual_required"
            task["expected_visual_targets"] = ["official poster images"]
            task["done_condition"] = (
                "Done when at least four images and eight vendors have official "
                "source records with caveats."
            )
            return response

        with mock.patch.dict(
            "os.environ",
            {CODEX_SEMANTIC_PLANNER_VALIDATION_MAX_ATTEMPTS_ENV: "2"},
        ):
            result, adapter_request = self.prepare_with_codex_adapter(
                "Compare official public poster image guidance across agencies.",
                requirement_types=("subject", "source_quality", "visual_modality"),
                visual_angle_indexes=(2, 3),
                planner_response_mutator=always_over_cap,
            )
        run_dir = Path(result["run_dir"])
        convergence = self.load_json(run_dir / SEMANTIC_PLANNER_CONVERGENCE_FILENAME)
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )

        self.assertEqual(result["status"], "blocked_semantic_planner_unavailable")
        self.assertEqual(convergence["status"], "blocked_semantic_planner_unavailable")
        self.assertEqual(convergence["attempt_count"], 2)
        self.assertEqual(len(adapter_request["_planner_requests"]), 4)
        self.assertTrue(convergence["attempts"][-1]["terminal_failure"])
        self.assertEqual(
            convergence["attempts"][-1]["repair_inputs"]["terminal_reason"],
            "max_attempts_exhausted",
        )
        self.assertIn(
            "bounded_task_requirement_exceeds_max_sources",
            convergence["terminal_failure"]["reason_codes"],
        )
        self.assertIn(
            "bounded_task_requirement_exceeds_max_images",
            convergence["terminal_failure"]["reason_codes"],
        )
        self.assertEqual(raw_response["failure_category"], "adapter_invalid_response")

    def test_visual_cap_repair_raises_image_budget_within_schema_limit(self) -> None:
        def planner_mutator(response: dict, _request: dict) -> dict:
            for task in response["candidate_plan"]["bounded_tasks"]:
                if task["route"] != "text_only":
                    task["max_images"] = 1
                    task["done_condition"] = (
                        "Done when three official images are compared with source-backed notes."
                    )
                    break
            return response

        result, _adapter_request = self.prepare_with_codex_adapter(
            "Compare official science diagram images with textual safety guidance.",
            requirement_types=("subject", "visual_modality", "source_quality"),
            visual_angle_indexes=(2, 3),
            planner_response_mutator=planner_mutator,
        )
        run_dir = Path(result["run_dir"])
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")["semantic_plan"]
        repaired_visual_tasks = [
            task
            for task in semantic_plan["bounded_tasks"]
            if task["route"] != "text_only" and task["max_images"] == 3
        ]

        self.assertEqual(result["status"], "awaiting_search_results")
        self.assertTrue(repaired_visual_tasks)
        materializations = raw_response[
            "candidate_plan_visual_image_cap_materializations"
        ]
        self.assertTrue(
            any(
                field.get("materialization") == "raised_visual_image_cap_within_budget"
                for materialization in materializations
                for field in materialization["fields"]
            ),
            materializations,
        )

    def test_source_budget_feasibility_repair_raises_task_source_cap(self) -> None:
        question = "Compare official provider documentation for permission behavior."

        def planner_mutator(response: dict, _request: dict) -> dict:
            task = response["candidate_plan"]["bounded_tasks"][0]
            task["max_sources"] = 2
            task["query"] = "Compare four vendors using official source records."
            task["done_condition"] = (
                "Done when four vendors have official source records and caveats."
            )
            return response

        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            requirement_types=("subject", "source_quality"),
            visual_angle_indexes=(),
            planner_response_mutator=planner_mutator,
        )
        run_dir = Path(result["run_dir"])
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")["semantic_plan"]
        first_task = semantic_plan["bounded_tasks"][0]

        self.assertEqual(result["status"], "awaiting_search_results")
        self.assertGreaterEqual(first_task["max_sources"], 4)
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=semantic_plan,
        )
        self.assertTrue(validation["ok"], validation)

    def test_source_cap_repair_splits_requirement_exceeding_schema_max(self) -> None:
        question = (
            "Compare OAuth device-flow provider behavior across official "
            "implementation documentation."
        )
        candidate = self.text_only_oauth_candidate(question=question)
        original_task_id = candidate["bounded_tasks"][0]["task_id"]
        candidate["bounded_tasks"][0]["max_sources"] = 5
        candidate["bounded_tasks"][0]["query"] = (
            "Compare OAuth device-flow provider behavior using at least 12 "
            "official sources."
        )
        candidate["bounded_tasks"][0]["done_condition"] = (
            "Complete when 12 official sources are checked with caveats."
        )

        original_validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
        )
        self.assertIn(
            "bounded_task_requirement_exceeds_max_sources",
            {failure["code"] for failure in original_validation["failures"]},
        )

        normalized, _cap_repairs = _normalize_candidate_executable_source_caps(
            candidate
        )
        repaired, materializations = _materialize_candidate_source_cap_splits(
            normalized
        )

        self.assertTrue(materializations)
        self.assertEqual(
            materializations[0]["materialization"],
            "split_overbroad_source_requirement",
        )
        split_task_ids = materializations[0]["split_task_ids"]
        repaired_task_ids = {
            task["task_id"] for task in repaired["bounded_tasks"]
        }
        self.assertNotIn(original_task_id, repaired_task_ids)
        self.assertTrue(set(split_task_ids).issubset(repaired_task_ids))
        for task in repaired["bounded_tasks"]:
            self.assertLessEqual(task["max_sources"], 5)
        for requirement in repaired["requirement_coverage_map"]:
            covered = set(requirement["covered_by_task_ids"])
            self.assertNotIn(original_task_id, covered)
            self.assertTrue(set(split_task_ids).issubset(covered))

        repaired_validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=repaired,
        )
        self.assertTrue(repaired_validation["ok"], repaired_validation)

    def test_source_cap_repair_keeps_unsplittable_over_cap_requirement_blocked(self) -> None:
        question = (
            "Compare OAuth device-flow provider behavior across official "
            "implementation documentation."
        )
        candidate = self.text_only_oauth_candidate(question=question)
        while len(candidate["bounded_tasks"]) < 39:
            next_index = len(candidate["bounded_tasks"]) + 1
            template = candidate["bounded_tasks"][
                (next_index - 1) % len(candidate["bounded_tasks"])
            ]
            task = json.loads(json.dumps(template))
            task["task_id"] = f"task_extra_{next_index:03d}"
            task["angle_id"] = candidate["angles"][
                (next_index - 1) % len(candidate["angles"])
            ]["angle_id"]
            task["query"] = (
                "OAuth device-flow provider documentation supplemental official "
                f"source task {next_index}"
            )
            task["done_condition"] = (
                "Stop when source-backed supplemental guidance notes are ready."
            )
            candidate["bounded_tasks"].append(task)

        original_task_id = candidate["bounded_tasks"][0]["task_id"]
        candidate["bounded_tasks"][0]["max_sources"] = 5
        candidate["bounded_tasks"][0]["query"] = (
            "Compare OAuth device-flow provider behavior using at least 12 "
            "official sources."
        )
        candidate["bounded_tasks"][0]["done_condition"] = (
            "Complete when 12 official sources are checked with caveats."
        )

        normalized, _cap_repairs = _normalize_candidate_executable_source_caps(
            candidate
        )
        split_repaired, split_materializations = _materialize_candidate_source_cap_splits(
            normalized
        )
        feasibility_repaired, feasibility_materializations = (
            _materialize_candidate_task_source_cap_feasibility(split_repaired)
        )
        repaired_task = next(
            task
            for task in feasibility_repaired["bounded_tasks"]
            if task["task_id"] == original_task_id
        )
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=feasibility_repaired,
        )
        failure_codes = {failure["code"] for failure in validation["failures"]}

        self.assertEqual(len(feasibility_repaired["bounded_tasks"]), 39)
        self.assertTrue(
            any(
                materialization.get("materialization")
                == "source_cap_split_blocked_task_ceiling"
                and materialization.get("blocked_feasibility_code")
                == "bounded_task_requirement_exceeds_max_sources"
                for materialization in split_materializations
            ),
            split_materializations,
        )
        self.assertTrue(
            any(
                materialization.get("materialization")
                == "source_cap_feasibility_blocked"
                and materialization.get("blocked_reason")
                == "source_cap_split_would_exceed_task_ceiling"
                for materialization in feasibility_materializations
            ),
            feasibility_materializations,
        )
        self.assertNotIn("source_pool_reuse_required", repaired_task)
        self.assertIn("12 official sources", repaired_task["query"])
        self.assertIn(
            "bounded_task_requirement_exceeds_max_sources",
            failure_codes,
        )

    def test_sem_reg_012_like_retry_records_scope_downgrade_and_source_split(self) -> None:
        question = (
            "Research cache invalidation implementation hazards for a Next.js "
            "migration using current official docs."
        )
        repair_marker = "nextjs cache invalidation comparison matrix repaired"

        def sanitize_text_only_nextjs(candidate: dict) -> None:
            angle_specs = [
                (
                    "primary_source",
                    "Current Next.js Cache Invalidation Source Baseline",
                    "Which current official Next.js docs define cache invalidation behavior for migration planning?",
                ),
                (
                    "implementation_detail",
                    "Next.js Cache Invalidation API Implementation Hazards",
                    "Which documented API constraints create implementation hazards during a Next.js migration?",
                ),
                (
                    "recent_change",
                    "Current Next.js Migration Freshness And Version Caveats",
                    "Which current official docs separate stable migration behavior from stale cache guidance?",
                ),
                (
                    "failure_pattern",
                    "Next.js Migration Failure Patterns And Remediation",
                    "Which cache invalidation failure patterns need source-backed remediation during migration?",
                ),
                (
                    "risk_or_guardrail",
                    "Next.js Cache Invalidation Risk Guardrails",
                    "Which official constraints become rollout guardrails for cache invalidation migration?",
                ),
            ]
            for index, angle in enumerate(candidate["angles"], start=1):
                need, title, research_question = angle_specs[(index - 1) % len(angle_specs)]
                angle["route"] = "text_only"
                angle["evidence_need"] = need
                angle["title"] = title
                angle["research_question"] = research_question
                angle["why_this_angle_matters"] = (
                    "This angle preserves a distinct Next.js cache migration evidence need."
                )
                angle["included_scope"] = [question]
                angle["excluded_scope"] = ["Do not use unrelated framework migration advice."]
                angle["expected_source_types"] = [
                    "current official Next.js documentation",
                    "official Vercel documentation",
                ]
                angle["expected_visual_targets"] = []
                angle["expected_artifacts"] = [
                    f"{title} source notes",
                    "migration hazard caveats",
                ]
                angle["search_queries"] = [f"{question} {need} official docs"]
                angle["success_criteria"] = [
                    "Use current official documentation as primary evidence.",
                    "Record caveats, deprecated behavior, and unknowns.",
                ]
                angle["report_section"] = f"Next.js Cache Migration Angle {index}"
                angle["risk_or_contradiction_checks"] = [
                    "Check stale, deprecated, experimental, or contradictory guidance."
                ]
            for index, task in enumerate(candidate["bounded_tasks"], start=1):
                task["route"] = "text_only"
                task["expected_visual_targets"] = []
                task["max_images"] = 0
                task.pop("expected_evidence", None)
                task["freshness_requirement"] = "recent"
                task["source_policy"] = {
                    "decision": "allowed",
                    "requires_official_or_primary": True,
                    "quality_requirements": [
                        "current official Next.js documentation",
                        "official Vercel documentation",
                    ],
                    "flags": [],
                }
                task["expected_source_types"] = [
                    "current official Next.js documentation",
                    "official Vercel documentation",
                ]
                task["expected_artifacts"] = [
                    "Next.js cache invalidation source notes",
                    "migration hazard caveats",
                ]
                task["success_criteria"] = [
                    "Use official, regulatory, or primary sources for support.",
                    "Tie each finding to the Next.js migration cache invalidation hazard.",
                ]
                task["query"] = (
                    f"{question} official documentation bounded task {index}"
                )
                task["done_condition"] = (
                    "Stop when source-backed Next.js cache migration findings, "
                    "caveats, and unknowns are recorded."
                )

        def planner_mutator(response: dict, request: dict) -> dict:
            response = json.loads(json.dumps(response))
            candidate = response["candidate_plan"]
            sanitize_text_only_nextjs(candidate)
            if not request.get("semantic_convergence_attempt"):
                return response

            kept_angles = candidate["angles"][:4]
            kept_angle_ids = [angle["angle_id"] for angle in kept_angles]
            kept_tasks = []
            for angle_id in kept_angle_ids:
                kept_tasks.extend(
                    [
                        task
                        for task in candidate["bounded_tasks"]
                        if task["angle_id"] == angle_id
                    ][:2]
                )
            candidate["question_scope"] = "narrow"
            candidate["angles"] = kept_angles
            candidate["bounded_tasks"] = kept_tasks
            candidate["constraints"].append(repair_marker)
            task_ids = [task["task_id"] for task in kept_tasks]
            for requirement in candidate["requirement_coverage_map"]:
                requirement["covered_by_angle_ids"] = kept_angle_ids
                requirement["covered_by_task_ids"] = task_ids
                requirement["coverage_status"] = "covered"
            overbroad_task = candidate["bounded_tasks"][-1]
            overbroad_task["max_sources"] = 5
            overbroad_task["query"] = (
                "Build a side-by-side Next.js cache invalidation migration "
                "hazard matrix from 20 official sources."
            )
            overbroad_task["expected_artifacts"] = [
                "side-by-side Next.js cache invalidation hazard matrix",
                "remediation next-action table",
            ]
            overbroad_task["success_criteria"] = [
                (
                    "Include match, partial, mismatch, unverifiable status, "
                    "evidence, caveat, and remediation fields."
                ),
                "Use official source-backed evidence for every row.",
            ]
            overbroad_task["done_condition"] = (
                "Complete when 20 official sources are checked for status, "
                "evidence, caveats, and remediation next actions."
            )
            return response

        def reviewer_mutator(response: dict, request: dict) -> dict:
            plan_text = json.dumps(request["semantic_plan"], ensure_ascii=False)
            if repair_marker not in plan_text:
                review = response["semantic_plan_review"]
                review["semantic_fit_score"] = 8.4
                review["blockers"] = [
                    {"code": "REQ_003_COMPARISON_DELIVERABLE_INCOMPLETE"}
                ]
                review["verdict"] = "release_ineligible"
            return response

        result, adapter_request = self.prepare_with_codex_adapter(
            question,
            route="text_only",
            requirement_types=("subject", "source_quality", "time_range"),
            planner_response_mutator=planner_mutator,
            reviewer_response_mutator=reviewer_mutator,
        )
        run_dir = Path(result["run_dir"])
        convergence = self.load_json(run_dir / SEMANTIC_PLANNER_CONVERGENCE_FILENAME)
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")[
            "semantic_plan"
        ]

        self.assertEqual(result["status"], "awaiting_search_results", result)
        self.assertEqual(convergence["status"], "converged", convergence)
        self.assertEqual(convergence["attempt_count"], 2)
        self.assertIn(
            "REQ_003_COMPARISON_DELIVERABLE_INCOMPLETE",
            convergence["attempts"][0]["reviewer_blocker_codes"],
        )
        self.assertIn(
            "candidate_plan_source_cap_split_materializations",
            raw_response,
        )
        self.assertIn(
            "candidate_plan_broad_cardinality_materializations",
            raw_response,
        )
        self.assertEqual(len(semantic_plan["angles"]), 4)
        self.assertGreaterEqual(len(semantic_plan["bounded_tasks"]), 6)
        self.assertLessEqual(len(semantic_plan["bounded_tasks"]), 12)
        self.assertEqual(semantic_plan["question_scope"], "medium")
        self.assertEqual(
            semantic_plan["scope_downgrade"]["status"],
            "oracle_bounded_semantic_scope_downgrade",
        )
        self.assertEqual(semantic_plan["scope_downgrade"]["from_scope"], "broad")
        self.assertEqual(semantic_plan["scope_downgrade"]["to_scope"], "medium")
        self.assertGreaterEqual(semantic_plan["scope_downgrade"]["retry_attempt"], 2)
        self.assertFalse(semantic_plan["scope_downgrade"]["generic_padding_added"])
        self.assertEqual(
            semantic_plan["question_class"],
            "implementation_architecture",
        )
        self.assertTrue(
            all(task["max_sources"] <= 5 for task in semantic_plan["bounded_tasks"])
        )
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=semantic_plan,
            raw_request=adapter_request["_planner_requests"][-1],
            visual_preference="text_only",
        )
        self.assertTrue(validation["ok"], validation)

    def test_source_cap_validation_ignores_task_id_range_reuse_references(self) -> None:
        question = "Compare OAuth device-flow provider behavior across official implementation documentation."
        candidate = self.text_only_oauth_candidate(question=question)
        task = candidate["bounded_tasks"][15]
        task["task_id"] = "task_016"
        task["max_sources"] = 5
        task["query"] = (
            "Compare OAuth device-flow implementation hazards across official provider "
            "documentation and define evidence-backed mitigations."
        )
        task["source_policy"] = {
            "allow_secondary": False,
            "policy": "Official provider documentation only; synthesis may reuse tasks 013-015 sources.",
            "required_source_quality": ["official", "primary"],
        }
        task["success_criteria"] = [
            "Include at least four hazards involving defaults, mutation contexts, or router boundaries.",
            "Give each hazard a mitigation and acceptance check.",
        ]

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
        )

        self.assertNotIn(
            "bounded_task_requirement_exceeds_max_sources",
            {failure["code"] for failure in validation["failures"]},
        )

    def test_source_cap_validation_still_rejects_true_source_count_over_cap(self) -> None:
        question = "Research cache invalidation implementation hazards for a Next.js migration."
        candidate = self.text_only_oauth_candidate(question=question)
        task = candidate["bounded_tasks"][0]
        task["max_sources"] = 5
        task["source_policy"] = {
            "allow_secondary": False,
            "policy": "Use at least 15 sources from official documentation families.",
            "required_source_quality": ["official", "primary"],
        }

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
        )

        self.assertFalse(validation["ok"], validation)
        self.assertIn(
            "bounded_task_requirement_exceeds_max_sources",
            {failure["code"] for failure in validation["failures"]},
        )

    def test_ambiguous_architecture_model_testing_classification_preserves_domain(self) -> None:
        self.assertNotEqual(
            classify_question(
                "Review hospital architecture model testing evidence from official facility guidance."
            ),
            "implementation_architecture",
        )
        self.assertEqual(
            classify_question(
                "Review Codex DeepResearch semantic planner architecture and testing strategy."
            ),
            "implementation_architecture",
        )

    def test_planner_code_does_not_special_case_semantic_regression_prompts(self) -> None:
        manifest_path = (
            ROOT
            / "plugins"
            / "codex-deepresearch"
            / "validation"
            / "semantic_regression_prompts.json"
        )
        manifest = self.load_json(manifest_path)
        code_text = "\n".join(
            [
                (
                    ROOT
                    / "plugins"
                    / "codex-deepresearch"
                    / "src"
                    / "deepresearch"
                    / "semantic_planner.py"
                ).read_text(encoding="utf-8"),
                (
                    ROOT
                    / "plugins"
                    / "codex-deepresearch"
                    / "src"
                    / "deepresearch"
                    / "search_handoff.py"
                ).read_text(encoding="utf-8"),
            ]
        )

        for prompt in manifest["prompts"]:
            with self.subTest(prompt_id=prompt["id"]):
                self.assertNotIn(prompt["id"], code_text)
                self.assertNotIn(prompt["prompt"], code_text)

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

    def test_codex_semantic_retries_then_records_explicit_narrow_scope_downgrade(self) -> None:
        question = "Compare official product screenshots and chart images for onboarding workflows"

        result, adapter_request = self.prepare_with_codex_adapter(
            question,
            stdout_format="jsonl_item_text",
            oracle_question_scope="broad",
            question_scope="narrow",
            angle_count=4,
            tasks_per_angle=2,
            requirement_types=("subject", "source_quality", "visual_modality"),
            visual_angle_indexes=(2, 3),
        )

        self.assertEqual(result["status"], "awaiting_search_results", result)
        run_dir = Path(result["run_dir"])
        convergence = self.load_json(run_dir / SEMANTIC_PLANNER_CONVERGENCE_FILENAME)
        self.assertEqual(convergence["status"], "converged", convergence)
        self.assertEqual(convergence["attempt_count"], 1)
        self.assertEqual(len(adapter_request["_planner_requests"]), 2)
        self.assertEqual(adapter_request["_planner_requests"][1]["retry_attempt"], 2)
        self.assertTrue(
            (run_dir / "semantic_reviewer_raw" / "reviewer_request.json").exists()
        )
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )
        self.assertIn(
            "candidate_plan_broad_cardinality_materializations",
            raw_response,
        )
        materializations = raw_response["candidate_plan_broad_cardinality_materializations"]
        self.assertTrue(
            any(
                item.get("materialization")
                == "oracle_bounded_semantic_scope_downgrade"
                and item.get("content_added") is False
                for item in materializations
            ),
            materializations,
        )
        first_attempt = convergence["attempts"][0]["adapter_candidate_attempts"][0]
        self.assertIn(
            "broad_question_angle_count_out_of_range",
            first_attempt["deterministic_failure_codes"],
        )
        self.assertIn(
            "broad_cardinality_replan_required",
            first_attempt["deterministic_failure_codes"],
        )
        candidate_validation = raw_response["candidate_validation"]
        self.assertTrue(candidate_validation["ok"], candidate_validation)
        self.assertEqual(candidate_validation["declared_question_scope"], "narrow")
        self.assertEqual(candidate_validation["scope_tier"], "narrow")
        self.assertTrue(candidate_validation["scope_downgrade_valid"])
        self.assertEqual(candidate_validation["question_class"], "visual_style")
        self.assertEqual(candidate_validation["angle_count"], 4)
        self.assertEqual(candidate_validation["task_count"], 8)
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")[
            "semantic_plan"
        ]
        self.assertEqual(semantic_plan["question_scope"], "narrow")
        self.assertEqual(
            semantic_plan["scope_downgrade"]["status"],
            "oracle_bounded_semantic_scope_downgrade",
        )
        validation = self.load_json(run_dir / "semantic_planner_validation.json")
        self.assertTrue(validation["scope_downgrade_valid"], validation)
        retry_request = adapter_request["_planner_requests"][1]
        self.assertIn("locked_semantic_expectation_oracle", retry_request)
        self.assertEqual(
            retry_request["locked_semantic_expectation_oracle"]["question_scope"],
            "broad",
        )
        self.assertIn("locked semantic expectation oracle", retry_request["planner_retry_instructions"])
        self.assertIn("generic suffix padding", retry_request["planner_retry_instructions"])
        self.assertEqual(
            set(candidate_validation["expected_evidence_needs"]),
            {
                "primary_source",
                "visual_example",
                "visual_observation",
                "official_source",
            },
        )

    def test_codex_semantic_retries_then_records_explicit_medium_scope_downgrade(self) -> None:
        question = (
            "Compare official city planning implementation indicators and "
            "responsible agencies across local government plans"
        )

        result, adapter_request = self.prepare_with_codex_adapter(
            question,
            stdout_format="jsonl_item_text",
            oracle_question_scope="broad",
            question_scope="medium",
            angle_count=5,
            tasks_per_angle=2,
            requirement_types=("subject", "source_quality", "deliverable_shape"),
        )

        self.assertEqual(result["status"], "awaiting_search_results", result)
        self.assertEqual(len(adapter_request["_planner_requests"]), 2)
        first_request, retry_request = adapter_request["_planner_requests"]
        self.assertNotIn("retry_attempt", first_request)
        self.assertEqual(retry_request["retry_attempt"], 2)
        self.assertIn("locked_semantic_expectation_oracle", retry_request)
        self.assertEqual(
            retry_request["locked_semantic_expectation_oracle"]["question_scope"],
            "broad",
        )
        self.assertGreaterEqual(
            retry_request["locked_semantic_expectation_oracle"]["bounded_task_range"]["min"],
            20,
        )
        self.assertIn(
            "locked semantic expectation oracle",
            retry_request["planner_retry_instructions"],
        )
        self.assertIn("generic suffix padding", retry_request["planner_retry_instructions"])

        run_dir = Path(result["run_dir"])
        convergence = self.load_json(run_dir / SEMANTIC_PLANNER_CONVERGENCE_FILENAME)
        inner_attempts = convergence["attempts"][0]["adapter_candidate_attempts"]
        self.assertEqual([attempt["attempt"] for attempt in inner_attempts], [1, 2])
        self.assertIn(
            "broad_cardinality_replan_required",
            inner_attempts[0]["deterministic_failure_codes"],
        )

        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )
        materializations = raw_response["candidate_plan_broad_cardinality_materializations"]
        self.assertTrue(
            any(
                item.get("materialization")
                == "oracle_bounded_semantic_scope_downgrade"
                and item.get("final_question_scope") == "medium"
                and item.get("content_added") is False
                for item in materializations
            ),
            materializations,
        )
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")[
            "semantic_plan"
        ]
        self.assertEqual(semantic_plan["question_scope"], "medium")
        self.assertEqual(semantic_plan["scope_downgrade"]["to_scope"], "medium")
        self.assertEqual(len(semantic_plan["angles"]), 5)
        self.assertEqual(len(semantic_plan["bounded_tasks"]), 10)

        validation = self.load_json(run_dir / "semantic_planner_validation.json")
        self.assertTrue(validation["ok"], validation)
        self.assertEqual(validation["scope_tier"], "medium")
        self.assertTrue(validation["scope_downgrade_valid"], validation)

    def test_cli_prepare_accepts_release_compliant_scope_downgrade_adapter(self) -> None:
        cases = (
            (
                "medium",
                (
                    "Compare official city planning implementation indicators and "
                    "responsible agencies across local government plans"
                ),
                "text_only",
            ),
            (
                "narrow",
                "Compare official product screenshots and chart images for onboarding workflows",
                "visual_required",
            ),
            (
                "narrow",
                "Compare official product screenshots and chart images for onboarding workflows",
                "visual_optional",
            ),
        )
        for target_scope, question, route in cases:
            with self.subTest(target_scope=target_scope, route=route):
                temp_dir = self.temp_runs_dir()
                bin_dir = temp_dir / "bin"
                bin_dir.mkdir()
                self.write_fake_codex_executable(bin_dir)
                runs_dir = temp_dir / "runs"
                env = os.environ.copy()
                env.update(
                    {
                        "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
                        CODEX_SEMANTIC_PLANNER_COMMAND_ENV: "codex exec --json",
                        CODEX_SEMANTIC_ORACLE_COMMAND_ENV: "codex exec --json",
                        CODEX_SEMANTIC_REVIEWER_COMMAND_ENV: "codex exec --json",
                        CODEX_SEMANTIC_ADAPTER_CAPACITY_RETRY_BACKOFF_SECONDS_ENV: "0",
                        "CODEX_DEEPRESEARCH_TEST_SCOPE_DOWNGRADE": target_scope,
                    }
                )

                completed = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "plugins" / "codex-deepresearch" / "scripts" / "codex-deepresearch"),
                        "prepare",
                        question,
                        "--runs-dir",
                        str(runs_dir),
                        "--route",
                        route,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=60,
                )

                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout)
                self.assertEqual(result["status"], "awaiting_search_results", result)
                self.assertEqual(result["planner_mode"], PLANNER_MODE_CODEX_SEMANTIC)
                self.assertTrue(result["semantic_release_eligible"], result)

                run_dir = Path(result["run_dir"])
                raw_response = self.load_json(
                    run_dir / "semantic_planner_raw" / "planner_response.json"
                )
                raw_request = self.load_json(
                    run_dir / "semantic_planner_raw" / "planner_request.json"
                )
                semantic_plan = self.load_json(run_dir / "semantic_plan.json")[
                    "semantic_plan"
                ]
                validation = self.load_json(run_dir / "semantic_planner_validation.json")
                convergence = self.load_json(
                    run_dir / SEMANTIC_PLANNER_CONVERGENCE_FILENAME
                )

                self.assertEqual(
                    raw_response["provenance"]["adapter_command"],
                    ["codex", "exec", "--json"],
                )
                self.assertEqual(raw_request["retry_attempt"], 2)
                self.assertEqual(
                    raw_request["locked_semantic_expectation_oracle"]["question_scope"],
                    "broad",
                )
                self.assertGreaterEqual(
                    raw_request["locked_semantic_expectation_oracle"]["bounded_task_range"]["min"],
                    20,
                )
                self.assertIn(
                    "broad_cardinality_replan_required",
                    raw_request["previous_candidate_validation_failure_codes"],
                )
                self.assertIn(
                    "locked semantic expectation oracle",
                    raw_request["planner_retry_instructions"],
                )
                self.assertIn("generic suffix padding", raw_request["planner_retry_instructions"])
                self.assertEqual(convergence["status"], "converged", convergence)
                self.assertEqual(
                    [
                        attempt["attempt"]
                        for attempt in convergence["attempts"][0][
                            "adapter_candidate_attempts"
                        ]
                    ],
                    [1, 2],
                )
                self.assertEqual(semantic_plan["question_scope"], target_scope)
                self.assertEqual(
                    semantic_plan["scope_downgrade"]["status"],
                    "oracle_bounded_semantic_scope_downgrade",
                )
                self.assertEqual(semantic_plan["scope_downgrade"]["from_scope"], "broad")
                self.assertEqual(semantic_plan["scope_downgrade"]["to_scope"], target_scope)
                self.assertGreaterEqual(
                    semantic_plan["scope_downgrade"]["retry_attempt"],
                    2,
                )
                materializations = raw_response[
                    "candidate_plan_broad_cardinality_materializations"
                ]
                self.assertTrue(
                    any(
                        item.get("materialization")
                        == "oracle_bounded_semantic_scope_downgrade"
                        and item.get("content_added") is False
                        and item.get("final_question_scope") == target_scope
                        for item in materializations
                    ),
                    materializations,
                )
                self.assertTrue(validation["ok"], validation)
                self.assertEqual(validation["scope_tier"], target_scope)
                self.assertTrue(validation["scope_downgrade_valid"], validation)
                if route == "text_only":
                    self.assertTrue(
                        all(task["route"] == "text_only" for task in semantic_plan["bounded_tasks"])
                    )
                elif route == "visual_required":
                    self.assertTrue(
                        any(
                            task["route"] == "visual_required"
                            for task in semantic_plan["bounded_tasks"]
                        )
                    )
                elif route == "visual_optional":
                    self.assertTrue(
                        any(
                            task["route"] == "visual_optional"
                            for task in semantic_plan["bounded_tasks"]
                        )
                    )
                    self.assertFalse(
                        any(
                            task["route"] == "visual_required"
                            for task in semantic_plan["bounded_tasks"]
                        )
                    )

    def test_broad_cardinality_shortfall_requests_replan_without_generic_padding(self) -> None:
        question = "Compare official city planning implementation indicators across local plans"
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
            tasks_per_angle=2,
            requirement_types=("subject", "source_quality"),
        )
        original_tasks = json.loads(json.dumps(response["candidate_plan"]["bounded_tasks"]))

        candidate, materializations = _materialize_candidate_broad_cardinality(
            response["candidate_plan"],
            original_question=question,
            raw_request=request,
        )

        self.assertEqual(candidate["bounded_tasks"], original_tasks)
        self.assertEqual(len(candidate["bounded_tasks"]), 10)
        self.assertTrue(
            any(
                item.get("materialization") == "broad_cardinality_replan_required"
                and item.get("content_added") is False
                for item in materializations
            ),
            materializations,
        )
        forbidden_suffixes = {
            "official documentation baseline",
            "current behavior constraints",
            "migration hazard evidence",
            "remediation evidence",
            "implementation boundary check",
            "synthesis readiness check",
        }
        task_text = "\n".join(
            str(task.get("query") or "") for task in candidate["bounded_tasks"]
        ).lower()
        self.assertFalse(forbidden_suffixes & set(task_text.splitlines()))
        self.assertFalse(any(suffix in task_text for suffix in forbidden_suffixes))

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
        )
        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertIn("broad_cardinality_replan_required", failure_codes)
        self.assertIn("broad_question_task_count_out_of_range", failure_codes)

    def test_locked_medium_oracle_accepts_medium_candidate_without_broad_replan(self) -> None:
        question = (
            "테스트라는 말이 의료 진단검사 맥락에서 쓰일 때, 국내외 공식 "
            "가이드라인의 민감도와 특이도 보고 기준을 조사해줘."
        )
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "e" * 64,
            "locked_semantic_expectation_oracle": {
                "question_scope": "medium",
                "bounded_task_range": {
                    "min": 12,
                    "max": 18,
                    "depth_preset": "standard",
                },
            },
        }
        response = self.codex_adapter_response(
            request,
            question_scope="medium",
            angle_count=6,
            tasks_per_angle=3,
            requirement_types=("subject", "source_quality", "deliverable_shape"),
        )
        candidate_input = response["candidate_plan"]
        angle_specs = [
            ("primary_source", "민감도 특이도 정의 산식", "정의·산식 근거 노트"),
            ("official_source", "민감도 특이도 공식 가이드라인 원문", "공식 원문 근거표"),
            ("implementation_detail", "민감도 특이도 참조표준 임계값", "참조표준 적용 메모"),
            ("comparative_analysis", "민감도 특이도 국내외 보고 기준 비교", "국내외 비교표"),
            ("recent_change", "민감도 특이도 최신판 개정 이력", "최신성 확인표"),
            ("counter_evidence", "민감도 특이도 불일치 적용 한계", "한계와 미확인 사항 목록"),
        ]
        for index, angle in enumerate(candidate_input["angles"], start=1):
            need, title, artifact = angle_specs[index - 1]
            angle["route"] = "text_only"
            angle["evidence_need"] = need
            angle["title"] = title
            angle["research_question"] = f"{title}을 공식 문서 근거로 어떻게 확인할 수 있는가?"
            angle["expected_source_types"] = [
                "대한민국 정부·규제기관 공식 원문",
                "국제·해외 공식 가이드라인",
            ]
            angle["expected_visual_targets"] = []
            angle["expected_artifacts"] = [
                artifact,
                "출처별 인용 위치",
                "requested table or matrix",
            ]
            angle["success_criteria"] = [
                "공식 또는 일차 출처를 사용한다.",
                "민감도와 특이도 보고 기준에 직접 연결한다.",
                "Preserve the requested table or matrix shape.",
            ]
            angle["report_section"] = f"진단검사 보고 기준 {index}"
        for index, task in enumerate(candidate_input["bounded_tasks"], start=1):
            task["route"] = "text_only"
            task["query"] = f"{question} 공식 가이드라인 텍스트 근거 task {index}"
            task["expected_source_types"] = [
                "대한민국 정부·규제기관 공식 원문",
                "국제·해외 공식 가이드라인",
            ]
            task["source_policy"] = {
                "allow_secondary": False,
                "policy": "정부·규제기관의 공식 원문과 공식 링크만 핵심 근거로 사용한다.",
                "required_source_quality": ["공식 원문", "일차 출처", "현행 판본"],
            }
            task["expected_visual_targets"] = []
            task["expected_artifacts"] = [
                "공식 문서 근거 노트",
                "출처별 인용 위치",
                "requested table or matrix",
            ]
            task["success_criteria"] = [
                "공식 또는 일차 출처를 사용한다.",
                "민감도와 특이도 보고 기준에 직접 연결한다.",
                "Preserve the requested table or matrix shape.",
            ]
            task["max_images"] = 0
        kept_tasks = []
        for index, angle in enumerate(candidate_input["angles"], start=1):
            tasks_for_angle = [
                task
                for task in candidate_input["bounded_tasks"]
                if task["angle_id"] == angle["angle_id"]
            ]
            kept_tasks.extend(tasks_for_angle[: 3 if index <= 2 else 2])
        candidate_input["bounded_tasks"] = kept_tasks
        kept_task_ids = [task["task_id"] for task in kept_tasks]
        kept_angle_ids = [angle["angle_id"] for angle in candidate_input["angles"]]
        for requirement in candidate_input["requirement_coverage_map"]:
            requirement["covered_by_angle_ids"] = list(kept_angle_ids)
            requirement["covered_by_task_ids"] = list(kept_task_ids)
            requirement["coverage_status"] = "covered"

        candidate, materializations = _materialize_candidate_broad_cardinality(
            candidate_input,
            original_question=question,
            raw_request=request,
        )

        self.assertEqual(candidate["question_scope"], "medium")
        self.assertNotIn("scope_downgrade", candidate)
        self.assertFalse(
            any(
                item.get("materialization") == "broad_cardinality_replan_required"
                for item in materializations
            ),
            materializations,
        )
        alignment = candidate["diagnostics"][
            "locked_semantic_expectation_oracle_alignment"
        ]
        self.assertEqual(alignment["status"], "honored_locked_oracle_scope")
        self.assertEqual(alignment["locked_question_scope"], "medium")
        self.assertTrue(alignment["counts_fit_locked_scope"])
        self.assertTrue(alignment["oracle_coverage_complete"])
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
            visual_preference="text_only",
        )
        self.assertTrue(validation["ok"], validation)
        self.assertTrue(validation["locked_oracle_scope_alignment_valid"], validation)
        self.assertFalse(validation["effective_broad_question"], validation)
        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertNotIn("broad_cardinality_replan_required", failure_codes)
        self.assertNotIn("hidden_scope_downgrade_without_diagnostics", failure_codes)

    def test_locked_medium_oracle_rejects_silent_broad_candidate(self) -> None:
        question = (
            "테스트라는 말이 의료 진단검사 맥락에서 쓰일 때, 국내외 공식 "
            "가이드라인의 민감도와 특이도 보고 기준을 조사해줘."
        )
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "f" * 64,
            "locked_semantic_expectation_oracle": {
                "question_scope": "medium",
                "bounded_task_range": {
                    "min": 12,
                    "max": 18,
                    "depth_preset": "standard",
                },
            },
        }
        response = self.codex_adapter_response(
            request,
            question_scope="broad",
            angle_count=5,
            tasks_per_angle=4,
            requirement_types=("subject", "source_quality", "deliverable_shape"),
        )

        candidate, materializations = _materialize_candidate_broad_cardinality(
            response["candidate_plan"],
            original_question=question,
            raw_request=request,
        )

        self.assertEqual(candidate["question_scope"], "broad")
        self.assertTrue(
            any(
                item.get("materialization") == "locked_oracle_scope_alignment_required"
                for item in materializations
            ),
            materializations,
        )
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
            visual_preference="text_only",
        )
        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertIn("locked_oracle_scope_alignment_failed", failure_codes)
        self.assertFalse(validation["ok"], validation)
        self.assertFalse(validation["locked_oracle_scope_alignment_valid"], validation)

    def test_locked_broad_oracle_rejects_medium_candidate_without_scope_downgrade(
        self,
    ) -> None:
        question = "Compare official city planning implementation indicators across local plans"
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "a" * 64,
            "locked_semantic_expectation_oracle": {
                "question_scope": "broad",
                "bounded_task_range": {
                    "min": 20,
                    "max": 40,
                    "depth_preset": "standard",
                },
            },
        }
        response = self.codex_adapter_response(
            request,
            question_scope="medium",
            angle_count=5,
            tasks_per_angle=2,
            requirement_types=("subject", "source_quality", "deliverable_shape"),
        )
        candidate_input = self.sanitize_city_planning_candidate_for_text_only(
            response["candidate_plan"],
            question=question,
        )

        candidate, materializations = _materialize_candidate_broad_cardinality(
            candidate_input,
            original_question=question,
            raw_request=request,
        )

        self.assertEqual(candidate["question_scope"], "medium")
        self.assertTrue(
            any(
                item.get("materialization") == "broad_cardinality_replan_required"
                and item.get("content_added") is False
                and item.get("target_scope_if_downgraded") == "medium"
                for item in materializations
            ),
            materializations,
        )
        self.assertIn(
            "broad_locked_semantic_expectation_oracle_scope",
            candidate["diagnostics"],
        )
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
            visual_preference="text_only",
        )
        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertIn("broad_locked_oracle_scope_downgrade_missing", failure_codes)
        self.assertFalse(validation["ok"], validation)
        self.assertFalse(validation["broad_locked_oracle_scope_valid"], validation)

    def test_locked_broad_oracle_rejects_first_attempt_candidate_forged_scope_downgrade(
        self,
    ) -> None:
        question = "Compare official city planning implementation indicators across local plans"
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "a" * 64,
            "locked_semantic_expectation_oracle": {
                "question_scope": "broad",
                "bounded_task_range": {
                    "min": 20,
                    "max": 40,
                    "depth_preset": "standard",
                },
            },
        }
        response = self.codex_adapter_response(
            request,
            question_scope="medium",
            angle_count=5,
            tasks_per_angle=2,
            requirement_types=("subject", "source_quality", "deliverable_shape"),
        )
        candidate_input = self.sanitize_city_planning_candidate_for_text_only(
            response["candidate_plan"],
            question=question,
        )
        candidate, _materializations = _materialize_candidate_broad_cardinality(
            candidate_input,
            original_question=question,
            raw_request=request,
        )
        forged_downgrade = {
            "status": "oracle_bounded_semantic_scope_downgrade",
            "from_scope": "broad",
            "to_scope": "medium",
            "retry_attempt": 2,
            "oracle_coverage_complete": True,
            "non_negotiable_coverage_complete": True,
            "generic_padding_added": False,
            "non_oracle_topics_added": False,
            "angle_count": 5,
            "task_count": 10,
            "final_scope_angle_range": [3, 6],
            "final_scope_task_range": [10, 19],
            "final_scope_min_tasks_per_angle": 1,
        }
        candidate["scope_downgrade"] = forged_downgrade
        candidate.setdefault("diagnostics", {})["scope_downgrade"] = forged_downgrade

        direct_validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
            visual_preference="text_only",
        )
        direct_failure_codes = {
            failure["code"] for failure in direct_validation["failures"]
        }
        self.assertIn(
            "invalid_scope_downgrade_diagnostics",
            direct_failure_codes,
        )
        self.assertFalse(
            direct_validation["scope_downgrade_valid"],
            direct_validation,
        )
        self.assertFalse(direct_validation["ok"], direct_validation)

        validation = _codex_semantic_candidate_validation(
            original_question=question,
            candidate=candidate,
            raw_request=request,
            visual_preference="text_only",
        )

        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertIn("invalid_scope_downgrade_diagnostics", failure_codes)
        self.assertIn("broad_locked_oracle_scope_downgrade_missing", failure_codes)
        self.assertFalse(validation["scope_downgrade_valid"], validation)
        self.assertFalse(validation["ok"], validation)

    def test_locked_broad_oracle_accepts_explicit_medium_scope_downgrade(self) -> None:
        question = "Compare official city planning implementation indicators across local plans"
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "b" * 64,
            "retry_attempt": 2,
            "locked_semantic_expectation_oracle": {
                "question_scope": "broad",
                "bounded_task_range": {
                    "min": 20,
                    "max": 40,
                    "depth_preset": "standard",
                },
            },
        }
        response = self.codex_adapter_response(
            request,
            question_scope="broad",
            angle_count=5,
            tasks_per_angle=2,
            requirement_types=("subject", "source_quality", "deliverable_shape"),
        )
        candidate_input = self.sanitize_city_planning_candidate_for_text_only(
            response["candidate_plan"],
            question=question,
        )

        candidate, materializations = _materialize_candidate_broad_cardinality(
            candidate_input,
            original_question=question,
            raw_request=request,
        )

        self.assertEqual(candidate["question_scope"], "medium")
        self.assertEqual(candidate["scope_downgrade"]["from_scope"], "broad")
        self.assertEqual(candidate["scope_downgrade"]["to_scope"], "medium")
        self.assertTrue(
            any(
                item.get("materialization")
                == "oracle_bounded_semantic_scope_downgrade"
                for item in materializations
            ),
            materializations,
        )
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
            raw_request=request,
            visual_preference="text_only",
        )
        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertNotIn("broad_locked_oracle_scope_downgrade_missing", failure_codes)
        self.assertTrue(validation["ok"], validation)
        self.assertTrue(validation["scope_downgrade_valid"], validation)
        self.assertTrue(validation["broad_locked_oracle_scope_valid"], validation)

    def test_scope_downgrade_requires_complete_oracle_coverage(self) -> None:
        question = "Compare official city planning implementation indicators across local plans"
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "c" * 64,
            "retry_attempt": 2,
        }
        response = self.codex_adapter_response(
            request,
            question_scope="medium",
            angle_count=5,
            tasks_per_angle=2,
            requirement_types=("subject", "source_quality"),
        )
        candidate_input = self.reduce_candidate_to_scope(
            response["candidate_plan"],
            angle_count=5,
            tasks_per_angle=2,
            question_scope="medium",
            coverage_complete=False,
        )

        candidate, materializations = _materialize_candidate_broad_cardinality(
            candidate_input,
            original_question=question,
            raw_request=request,
        )

        self.assertNotIn("scope_downgrade", candidate)
        self.assertTrue(
            any(
                item.get("materialization") == "broad_cardinality_replan_required"
                and item.get("oracle_coverage_complete") is False
                for item in materializations
            ),
            materializations,
        )
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
        )
        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertIn("requirement_not_covered", failure_codes)
        self.assertFalse(validation["scope_downgrade_valid"], validation)

    def test_text_only_scope_downgrade_preserves_text_only_tasks(self) -> None:
        question = (
            "Compare official city planning implementation indicators and "
            "responsible agencies across local government plans"
        )
        request = {
            "original_question": question,
            "depth_preset": "standard",
            "planner_adapter": "codex_native_semantic_candidate_adapter",
            "prompt_version": "p3-sp2-candidate-v2",
            "adapter_request_hash": "d" * 64,
            "retry_attempt": 2,
            "visual_preference": "text_only",
        }
        response = self.codex_adapter_response(
            request,
            question_scope="medium",
            angle_count=5,
            tasks_per_angle=2,
            requirement_types=("subject", "source_quality", "deliverable_shape"),
        )
        candidate = response["candidate_plan"]
        angle_specs = [
            {
                "need": "primary_source",
                "title": "Adopted Plan Metrics",
                "question": "Which adopted plans define measurable implementation indicators?",
                "artifact": "adopted plan metric inventory",
            },
            {
                "need": "official_source",
                "title": "Agency Responsibility Mapping",
                "question": "Which departments or partner agencies are assigned delivery responsibility?",
                "artifact": "agency responsibility matrix",
            },
            {
                "need": "recent_change",
                "title": "Update Cycle And Amendments",
                "question": "What recent amendments or annual reports changed the implementation baseline?",
                "artifact": "recent plan update timeline",
            },
            {
                "need": "comparative_analysis",
                "title": "Cross-Jurisdiction Differences",
                "question": "How do indicator definitions and agency roles differ across jurisdictions?",
                "artifact": "cross-jurisdiction comparison table",
            },
            {
                "need": "counter_evidence",
                "title": "Gaps And Conflicting Duties",
                "question": "Where do official records omit owners or assign overlapping responsibilities?",
                "artifact": "implementation gap register",
            },
        ]
        for index, angle in enumerate(candidate["angles"], start=1):
            spec = angle_specs[index - 1]
            angle["route"] = "text_only"
            angle["evidence_need"] = spec["need"]
            angle["title"] = spec["title"]
            angle["research_question"] = (
                f"{spec['question']} Scope: {question}."
            )
            angle["why_this_angle_matters"] = (
                "This preserves a distinct official planning evidence need."
            )
            angle["included_scope"] = [question]
            angle["excluded_scope"] = ["Do not add image, chart, or software implementation work."]
            angle["expected_source_types"] = ["official local government plans"]
            angle["expected_visual_targets"] = []
            angle["expected_artifacts"] = [
                spec["artifact"],
                "official source notes",
            ]
            angle["search_queries"] = [
                f"{question} {spec['need']} official local government plan"
            ]
            angle["success_criteria"] = [
                "Use official local government planning records.",
                "Record indicators, agencies, caveats, and unknowns.",
            ]
            angle["report_section"] = f"Planning Indicators {index}"
            angle["risk_or_contradiction_checks"] = [
                "Check stale plan years and conflicting responsibility assignments."
            ]
        for index, task in enumerate(candidate["bounded_tasks"], start=1):
            task["route"] = "text_only"
            task["query"] = f"{question} official planning records task {index}"
            task["expected_visual_targets"] = []
            task["expected_artifacts"] = [
                "planning indicator source notes",
                "responsible agency comparison table",
            ]
            task["success_criteria"] = [
                "Use official local government planning records.",
                "Record indicators, agencies, caveats, and unknowns.",
            ]
            task["max_images"] = 0
            task.pop("expected_evidence", None)

        downgraded, _materializations = _materialize_candidate_broad_cardinality(
            candidate,
            original_question=question,
            raw_request=request,
        )

        self.assertEqual(downgraded["question_scope"], "medium")
        self.assertTrue(downgraded["scope_downgrade"])
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=downgraded,
            raw_request=request,
            visual_preference="text_only",
        )
        self.assertTrue(validation["ok"], validation)
        self.assertTrue(validation["scope_downgrade_valid"], validation)
        self.assertTrue(
            all(task["route"] == "text_only" for task in downgraded["bounded_tasks"])
        )
        self.assertTrue(all(task["max_images"] == 0 for task in downgraded["bounded_tasks"]))

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
        question = self.semantic_regression_prompt(5)

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

    def test_codex_semantic_materializes_distinct_duplicate_report_sections(self) -> None:
        question = self.semantic_regression_prompt(16)
        duplicated_section = "Comparison of methods, metrics, and model purposes"

        def sem_reg_017_style_duplicate_sections(response: dict) -> dict:
            response = json.loads(json.dumps(response))
            candidate = response["candidate_plan"]
            angle_specs = [
                (
                    "Epidemiological Model Validation Terminology in Public Reports",
                    "Which official methodological sources define model validation terminology in public epidemiology reports?",
                    "official_source",
                    ["terminology source notes", "definition caveats"],
                    "Scope and terminology",
                ),
                (
                    "Validation Designs Used in Public Epidemiological Models",
                    "Which public epidemiology reports document validation splits, holdout data, or external validation datasets?",
                    "primary_source",
                    ["validation design inventory", "dataset independence notes"],
                    "Taxonomy of epidemiological validation approaches",
                ),
                (
                    "Calibration, Discrimination, Sensitivity, and Uncertainty in Epidemiology Reports",
                    "Which public epidemiology reports use calibration, discrimination, sensitivity, or uncertainty metrics for validation?",
                    "comparative_analysis",
                    ["metric comparison matrix", "method limitation notes"],
                    duplicated_section,
                ),
                (
                    "Validation Expectations Across Epidemiological Model Purposes",
                    "How do validation expectations differ for forecasting, burden estimation, policy simulation, and risk stratification reports?",
                    "risk_or_guardrail",
                    ["purpose-specific validation matrix", "decision caveat register"],
                    duplicated_section,
                ),
                (
                    "Scientific Epidemiological Model Validation Versus Software Deployment",
                    "Which authoritative sources separate scientific model validation claims from software deployment or runtime validation?",
                    "counter_evidence",
                    ["scope distinction notes", "software-deployment exclusion caveats"],
                    "Distinction from software deployment",
                ),
            ]
            for angle, (
                title,
                research_question,
                evidence_need,
                expected_artifacts,
                report_section,
            ) in zip(candidate["angles"], angle_specs):
                angle["route"] = "text_only"
                angle["expected_visual_targets"] = []
                angle["title"] = title
                angle["research_question"] = research_question
                angle["evidence_need"] = evidence_need
                angle["expected_artifacts"] = expected_artifacts
                angle["report_section"] = report_section
                angle["success_criteria"] = [
                    "Findings must preserve public epidemiology model-validation scope.",
                    "Do not treat software deployment validation as the requested subject.",
                ]
            return response

        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            stdout_format="jsonl_item_text",
            response_mutator=sem_reg_017_style_duplicate_sections,
            requirement_types=("subject", "source_quality"),
        )
        run_dir = Path(result["run_dir"])
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")[
            "semantic_plan"
        ]
        planner_validation = self.load_json(run_dir / "semantic_planner_validation.json")

        self.assertTrue(result["semantic_release_eligible"], result)
        self.assertTrue(planner_validation["ok"], planner_validation)
        materializations = raw_response[
            "candidate_plan_report_section_materializations"
        ]
        self.assertEqual(
            [item["angle_id"] for item in materializations],
            ["angle_003", "angle_004"],
        )
        sections = [angle["report_section"] for angle in semantic_plan["angles"]]
        self.assertEqual(len(sections), len(set(sections)))
        self.assertIn(
            "Calibration Discrimination Sensitivity And Uncertainty",
            sections,
        )
        self.assertIn(
            "Validation Expectations Across Epidemiological Model",
            sections,
        )
        failed_material_checks = [
            check
            for check in planner_validation["material_difference_checks"]
            if not check["valid"]
        ]
        self.assertEqual(failed_material_checks, [])

    def test_duplicate_report_section_materializer_preserves_true_duplicate_failure(self) -> None:
        question = self.semantic_regression_prompt(16)
        response = self.codex_adapter_response(
            build_codex_semantic_raw_request(question=question),
            requirement_types=("subject", "source_quality"),
        )
        candidate = json.loads(json.dumps(response["candidate_plan"]))
        source_angle = candidate["angles"][2]
        target_angle = candidate["angles"][3]
        for field_name in (
            "title",
            "research_question",
            "evidence_need",
            "expected_artifacts",
            "report_section",
        ):
            target_angle[field_name] = json.loads(json.dumps(source_angle[field_name]))

        repaired, materializations = _materialize_candidate_report_sections(
            candidate,
            original_question=question,
        )
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=repaired,
        )

        self.assertEqual(materializations, [])
        self.assertEqual(
            repaired["angles"][2]["report_section"],
            repaired["angles"][3]["report_section"],
        )
        self.assertIn(
            "semantic_angle_release_duplicate_failed",
            {failure["code"] for failure in validation["failures"]},
        )

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

    def test_convergence_records_inner_candidate_validation_retry_attempts(self) -> None:
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
        convergence = self.load_json(run_dir / SEMANTIC_PLANNER_CONVERGENCE_FILENAME)

        self.assertEqual(result["status"], "awaiting_search_results")
        self.assertEqual(len(adapter_request["_planner_requests"]), 3)
        self.assertEqual(convergence["attempt_count"], 1)
        outer_attempt = convergence["attempts"][0]
        self.assertEqual(outer_attempt["adapter_candidate_attempt_count"], 3)
        inner_attempts = outer_attempt["adapter_candidate_attempts"]
        self.assertEqual([attempt["attempt"] for attempt in inner_attempts], [1, 2, 3])
        required_fields = {
            "attempt",
            "candidate_id",
            "candidate_hash",
            "raw_response_hash",
            "deterministic_ok",
            "deterministic_failure_codes",
            "deterministic_failures",
            "repair_inputs",
            "final_selection",
            "terminal_failure",
        }
        for inner_attempt in inner_attempts:
            self.assertTrue(required_fields.issubset(inner_attempt), inner_attempt)
            self.assertEqual(len(inner_attempt["candidate_hash"]), 64)
            self.assertEqual(
                inner_attempt["candidate_id"],
                inner_attempt["candidate_hash"][:16],
            )
        self.assertFalse(inner_attempts[0]["deterministic_ok"])
        self.assertIn(
            "broad_angle_has_too_few_tasks",
            inner_attempts[0]["deterministic_failure_codes"],
        )
        self.assertEqual(
            inner_attempts[0]["repair_inputs"]["retry_source"],
            "adapter_candidate_validation",
        )
        self.assertEqual(inner_attempts[0]["repair_inputs"]["next_attempt"], 2)
        self.assertFalse(inner_attempts[1]["deterministic_ok"])
        self.assertEqual(inner_attempts[1]["repair_inputs"]["next_attempt"], 3)
        self.assertTrue(inner_attempts[2]["deterministic_ok"])
        self.assertTrue(inner_attempts[2]["final_selection"])
        self.assertEqual(inner_attempts[2]["repair_inputs"], {})

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

    def test_codex_semantic_capacity_retry_preserves_release_eligible_adapters(self) -> None:
        question = "Research official public health poster image guidance in Korea"
        result, adapter_request = self.prepare_with_codex_adapter(
            question,
            requirement_types=("subject", "source_quality", "visual_modality"),
            visual_angle_indexes=(2, 3),
            capacity_failures_by_artifact={
                "semantic_oracle_raw_request": 1,
                "semantic_planner_raw_request": 1,
                "semantic_reviewer_raw_request": 1,
            },
        )
        run_dir = Path(result["run_dir"])
        evidence = self.load_json(run_dir / "evidence.json")
        validation = self.load_json(run_dir / "semantic_planner_validation.json")
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")["semantic_plan"]
        raw_responses = (
            self.load_json(run_dir / "semantic_oracle_raw" / "oracle_response.json"),
            self.load_json(run_dir / "semantic_planner_raw" / "planner_response.json"),
            self.load_json(run_dir / "semantic_reviewer_raw" / "reviewer_response.json"),
        )

        self.assertEqual(
            adapter_request["_artifact_types"].count("semantic_oracle_raw_request"),
            2,
        )
        self.assertEqual(
            adapter_request["_artifact_types"].count("semantic_planner_raw_request"),
            2,
        )
        self.assertEqual(
            adapter_request["_artifact_types"].count("semantic_reviewer_raw_request"),
            2,
        )
        self.assertTrue(validation["ok"], validation)
        self.assertTrue(evidence["semantic_planner"]["semantic_release_eligible"])
        self.assertEqual(evidence["semantic_planner"]["status"], "semantic_review_passed")

        for raw_response in raw_responses:
            provenance = raw_response["provenance"]
            self.assertFalse(provenance.get("non_release_fixture"), raw_response)
            retry = provenance["adapter_retry_metadata"]
            self.assertEqual(retry["transient_failure_type"], "model_capacity")
            self.assertEqual(retry["attempt_count"], 2)
            self.assertEqual(retry["failed_attempt_count"], 1)
            self.assertEqual(retry["successful_attempt"], 2)
            self.assertEqual(retry["failed_attempts"][0]["failure_category"], "model_capacity")
            self.assertIn(
                "Selected model is at capacity",
                retry["failed_attempts"][0]["stderr_preview"],
            )
            self.assertEqual(raw_response["adapter_retry_metadata"], retry)

        user_visible_constraints = json.dumps(
            semantic_plan["constraints"],
            ensure_ascii=False,
        ).lower()
        self.assertNotIn("capacity", user_visible_constraints)
        self.assertNotIn("retry", user_visible_constraints)

    def test_codex_semantic_non_capacity_oracle_failure_still_uses_non_release_fixture(self) -> None:
        result, adapter_request = self.prepare_with_codex_adapter(
            "Research official public health guidance in Korea",
            requirement_types=("subject", "source_quality"),
            non_capacity_failures_by_artifact={"semantic_oracle_raw_request": 1},
        )
        run_dir = Path(result["run_dir"])
        oracle_raw_response = self.load_json(
            run_dir / "semantic_oracle_raw" / "oracle_response.json"
        )
        review = self.load_json(run_dir / "semantic_plan_review.json")

        self.assertEqual(
            adapter_request["_artifact_types"].count("semantic_oracle_raw_request"),
            1,
        )
        self.assertEqual(result["status"], "blocked_semantic_review_failed")
        self.assertEqual(
            oracle_raw_response["oracle_adapter"],
            "deterministic_semantic_oracle_fixture_non_release",
        )
        self.assertTrue(oracle_raw_response["provenance"]["non_release_fixture"])
        self.assertNotIn(
            "adapter_retry_metadata",
            oracle_raw_response["provenance"],
        )
        blocker_codes = {blocker["code"] for blocker in review["blockers"]}
        self.assertIn("non_release_oracle_fixture", blocker_codes)

    def test_codex_semantic_sem_reg_004_oracle_capacity_retry_avoids_fixture(self) -> None:
        question = self.semantic_regression_prompt(3)

        def bounded_visual_optional_response(response: dict, _request: dict) -> dict:
            response = json.loads(json.dumps(response))
            candidate = response["candidate_plan"]
            for angle in candidate["angles"]:
                if angle["angle_id"] == "angle_002":
                    angle["route"] = "visual_optional"
                    angle["evidence_need"] = "visual_example"
                    angle["expected_visual_targets"] = [
                        "공식 문서에 포함된 건축 모델 산출물 예시가 있는 경우"
                    ]
                    continue
                angle["route"] = "text_only"
                angle["expected_visual_targets"] = []
                if angle["evidence_need"] in {"visual_example", "visual_observation"}:
                    angle["evidence_need"] = "official_source"
                angle["expected_artifacts"] = [
                    f"{angle['angle_id']} 공식 기준 및 입찰 문서 비교 notes"
                ]
                angle["success_criteria"] = [
                    f"공식 텍스트 또는 문서 근거로 {question} 요구를 보존한다."
                ]
            for task in candidate["bounded_tasks"]:
                if task["angle_id"] == "angle_002":
                    task["route"] = "visual_optional"
                    task["expected_visual_targets"] = [
                        "공식 문서에 포함된 건축 모델 산출물 예시가 있는 경우"
                    ]
                    task["max_images"] = 1
                    task["expected_evidence"] = (
                        ["visual_observation"]
                        if task["task_id"].endswith(("006", "008"))
                        else ["visual_example"]
                    )
                    continue
                task["route"] = "text_only"
                task["expected_visual_targets"] = []
                task["max_images"] = 0
                task.pop("expected_evidence", None)
                task["query"] = (
                    f"{question} 공식 텍스트 문서 기준 비교 task {task['task_id']}"
                )
                task["expected_artifacts"] = [
                    f"{task['task_id']} 공식 기준 및 입찰 문서 근거 notes",
                    "requested table or matrix",
                ]
                task["success_criteria"] = [
                    f"Task must preserve subject: {question}.",
                    "Use official text or document sources as evidence.",
                    "Preserve the requested table or matrix shape.",
                ]
                task["done_condition"] = (
                    "Stop when text or document evidence and requested table details are recorded."
                )
            return response

        result, adapter_request = self.prepare_with_codex_adapter(
            question,
            route="visual_optional",
            requirement_types=("subject", "source_quality", "visual_modality", "deliverable_shape"),
            visual_angle_indexes=(2,),
            capacity_failures_by_artifact={"semantic_oracle_raw_request": 1},
            planner_response_mutator=bounded_visual_optional_response,
        )
        run_dir = Path(result["run_dir"])
        oracle_raw_response = self.load_json(
            run_dir / "semantic_oracle_raw" / "oracle_response.json"
        )
        review = self.load_json(run_dir / "semantic_plan_review.json")
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")["semantic_plan"]

        self.assertEqual(result["status"], "awaiting_search_results", result)
        self.assertEqual(result["semantic_planning_status"], "semantic_review_passed")
        self.assertTrue(result["semantic_release_eligible"], result)
        self.assertEqual(
            adapter_request["_artifact_types"].count("semantic_oracle_raw_request"),
            2,
        )
        self.assertEqual(
            oracle_raw_response["oracle_adapter"],
            "codex_native_semantic_expectation_oracle",
        )
        self.assertFalse(oracle_raw_response["provenance"].get("non_release_fixture"))
        self.assertIn("adapter_retry_metadata", oracle_raw_response["provenance"])
        self.assertTrue(review["semantic_release_eligible"], review)
        self.assertEqual(review["reviewer_independence"]["status"], "passed")
        self.assertNotIn(
            "non_release_oracle_fixture",
            {blocker["code"] for blocker in review["blockers"]},
        )
        self.assertTrue(
            any(task.get("route") != "text_only" for task in semantic_plan["bounded_tasks"])
        )

    def test_codex_semantic_materializes_sem_reg_004_visual_expected_evidence(self) -> None:
        question = self.semantic_regression_prompt(1)
        mixed_text_only_task: dict[str, str] = {}

        def sem_reg_004_style_missing_expected_evidence(response: dict) -> dict:
            response = json.loads(json.dumps(response))
            for angle in response["candidate_plan"]["angles"]:
                if angle["route"] != "text_only":
                    angle["route"] = "visual_optional"
                    angle["expected_visual_targets"] = [
                        "한국 공공보건 포스터 이미지의 손씻기 지침과 시각적 안내 요소"
                    ]
            for task in response["candidate_plan"]["bounded_tasks"]:
                task.pop("expected_evidence", None)
                if task["route"] != "text_only":
                    if not mixed_text_only_task:
                        mixed_text_only_task["task_id"] = task["task_id"]
                        task["route"] = "text_only"
                        task["max_images"] = 0
                        task["expected_visual_targets"] = []
                        task["expected_artifacts"] = ["official text source notes"]
                        task["success_criteria"] = [
                            "Collect official text source support only.",
                        ]
                        continue
                    task["route"] = "visual_optional"
                    task["expected_visual_targets"] = [
                        "한국 공공보건 포스터 이미지의 손씻기 지침과 시각적 안내 요소"
                    ]
                    task["expected_artifacts"] = [
                        *task["expected_artifacts"],
                        "representative poster image examples",
                        "visual observation notes",
                    ]
                    task["success_criteria"] = [
                        *task["success_criteria"],
                        "Analyze visible poster structure and handwashing guidance.",
                    ]
            return response

        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            route="visual_optional",
            requirement_types=("subject", "source_quality", "visual_modality"),
            visual_angle_indexes=(2, 3),
            response_mutator=sem_reg_004_style_missing_expected_evidence,
        )
        run_dir = Path(result["run_dir"])
        semantic_plan = self.load_json(run_dir / "semantic_plan.json")[
            "semantic_plan"
        ]
        validation = self.load_json(run_dir / "semantic_planner_validation.json")
        raw_response = self.load_json(
            run_dir / "semantic_planner_raw" / "planner_response.json"
        )
        search_tasks = self.load_json(run_dir / "search_tasks.json")["tasks"]
        planned_research_tasks = plan_research_tasks(run=run_dir, min_tasks=1)["tasks"]
        visual_tasks = [
            task for task in semantic_plan["bounded_tasks"]
            if task["route"] != "text_only"
        ]
        text_only_tasks = [
            task for task in semantic_plan["bounded_tasks"]
            if task["route"] == "text_only"
        ]

        self.assertEqual(result["status"], "awaiting_search_results", result)
        self.assertTrue(result["semantic_release_eligible"], result)
        self.assertTrue(validation["semantic_release_eligible"], validation)
        self.assertTrue(validation["ok"], validation)
        self.assertGreaterEqual(
            validation["visual_expected_evidence_hits"].get("visual_example", 0),
            1,
        )
        self.assertGreaterEqual(
            validation["visual_expected_evidence_hits"].get(
                "visual_observation",
                0,
            ),
            1,
        )
        self.assertTrue(visual_tasks)
        self.assertTrue(
            all(task.get("expected_evidence") for task in visual_tasks),
            visual_tasks,
        )
        visual_expected = {
            item
            for task in visual_tasks
            for item in task.get("expected_evidence", [])
        }
        self.assertIn("visual_example", visual_expected)
        self.assertIn("visual_observation", visual_expected)
        self.assertFalse(
            [
                task
                for task in text_only_tasks
                if task.get("expected_evidence")
            ],
            text_only_tasks,
        )
        self.assertTrue(mixed_text_only_task, "mixed text-only task was not created")
        self.assertFalse(
            [
                task
                for task in text_only_tasks
                if task["task_id"] == mixed_text_only_task["task_id"]
                and task.get("expected_evidence")
            ],
            text_only_tasks,
        )
        self.assertIn(
            "candidate_plan_expected_evidence_materializations",
            raw_response,
        )
        inferred_ids = {
            record["task_id"]
            for record in raw_response[
                "candidate_plan_expected_evidence_materializations"
            ]
            if record["materialization"]
            == "inferred_expected_evidence_from_visual_context"
        }
        self.assertNotIn(mixed_text_only_task["task_id"], inferred_ids)
        self.assertTrue(
            {task["task_id"] for task in visual_tasks}.issubset(inferred_ids)
        )
        expected_by_task_id = {
            task["task_id"]: task["expected_evidence"]
            for task in visual_tasks
        }
        for task_collection in (search_tasks, planned_research_tasks):
            for task in task_collection:
                if task["task_id"] in expected_by_task_id:
                    self.assertTrue(
                        set(expected_by_task_id[task["task_id"]]).issubset(
                            set(task["expected_evidence"])
                        ),
                        task,
                    )

    def test_codex_semantic_rejects_text_only_visual_expected_evidence(self) -> None:
        def inject_text_only_visual_expected_evidence(response: dict) -> dict:
            response = json.loads(json.dumps(response))
            text_task = next(
                task
                for task in response["candidate_plan"]["bounded_tasks"]
                if task["route"] == "text_only"
            )
            text_task["expected_evidence"] = ["visual_example", "vlm_analysis"]
            return response

        result, _adapter_request = self.prepare_with_codex_adapter(
            "Research official public health guidance in Korea",
            requirement_types=("subject", "source_quality"),
            response_mutator=inject_text_only_visual_expected_evidence,
        )
        self.assert_invalid_adapter_response_blocked(
            result,
            expected_failure_codes={"text_only_task_visual_expected_evidence"},
        )

    def test_validate_semantic_candidate_plan_rejects_falsy_route_visual_expected_evidence(self) -> None:
        question = "Research official public health poster image guidance in Korea"
        result, _adapter_request = self.prepare_with_codex_adapter(
            question,
            requirement_types=("subject", "source_quality", "visual_modality"),
            visual_angle_indexes=(2, 3),
        )
        run_dir = Path(result["run_dir"])
        plan = self.load_json(run_dir / "semantic_plan.json")["semantic_plan"]
        text_task = next(
            task for task in plan["bounded_tasks"] if task["route"] == "text_only"
        )
        text_task["route"] = ""
        text_task["expected_evidence"] = ["visual_example"]

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=plan,
        )

        self.assertFalse(validation["ok"], validation)
        self.assertIn(
            "text_only_task_visual_expected_evidence",
            {failure["code"] for failure in validation["failures"]},
        )

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
            response["candidate_plan"]["constraints"] = [
                *response["candidate_plan"].get("constraints", []),
                (
                    "Overall source budget is 20 and each bounded task max_sources "
                    "must remain 1 to force reuse."
                ),
            ]
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
        self.assertIn(
            "candidate_plan_source_cap_constraint_materializations",
            raw_response,
        )
        normalized_ids = {
            record["task_id"]
            for record in raw_response["candidate_plan_source_cap_normalizations"]
        }
        self.assertTrue(set(multi_source_tasks).issubset(normalized_ids))
        constraints_text = json.dumps(
            semantic_plan["constraints"],
            ensure_ascii=False,
        )
        self.assertIn("bounded_tasks.max_sources is authoritative", constraints_text)
        self.assertNotIn("must remain 1", constraints_text)
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

    def test_source_cap_constraint_materialization_preserves_global_budget_as_runner_budget(self) -> None:
        question = "Compare official agency permit evidence across Korea municipalities"
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
        )
        candidate = response["candidate_plan"]
        for task in candidate["bounded_tasks"]:
            task["max_sources"] = 3
        candidate["constraints"].append(
            "Global source budget: task max_sources sum <=20, total max 20 "
            "sources, and one decisive source per task."
        )

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
        )
        self.assertFalse(validation["ok"])
        self.assertIn(
            "source_cap_constraint_conflicts_with_tasks",
            {failure["code"] for failure in validation["failures"]},
        )

        normalized, cap_repairs = _normalize_candidate_executable_source_caps(candidate)
        self.assertEqual(cap_repairs, [])
        repaired, materializations = _materialize_candidate_source_cap_constraints(
            normalized,
            source_cap_normalizations=cap_repairs,
        )

        self.assertTrue(materializations)
        constraints_text = json.dumps(repaired["constraints"], ensure_ascii=False)
        self.assertIn("bounded_tasks.max_sources is authoritative", constraints_text)
        self.assertIn("max_unique_sources=20", constraints_text)
        self.assertIn("runner-level unique-source reuse budget", constraints_text)
        self.assertNotIn("sum <=20", constraints_text)
        self.assertNotIn("one decisive source per task", constraints_text)
        self.assertNotIn("must not override", constraints_text)
        self.assertEqual(repaired["runner_source_budget"]["max_unique_sources"], 20)
        self.assertEqual(repaired["runner_source_budget"]["declared_source_budget"], 20)
        self.assertEqual(repaired["runner_source_budget"]["task_max_sources_sum"], 60)
        self.assertTrue(repaired["runner_source_budget"]["reuse_required"])
        self.assertEqual(
            materializations[0]["preserved_declared_source_budget"],
            20,
        )
        self.assertEqual(
            materializations[0]["runner_source_budget"]["max_unique_sources"],
            20,
        )

        missing_metadata = json.loads(json.dumps(repaired))
        missing_metadata.pop("runner_source_budget")
        missing_metadata_validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=missing_metadata,
        )
        self.assertFalse(missing_metadata_validation["ok"], missing_metadata_validation)
        self.assertIn(
            "global_source_budget_missing_executable_runner_budget",
            {failure["code"] for failure in missing_metadata_validation["failures"]},
        )

        missing_numeric_cap = json.loads(json.dumps(repaired))
        missing_numeric_cap["constraints"] = [
            (
                "Executable source caps are task-specific: bounded_tasks.max_sources "
                "is authoritative after source-cap consistency repair. Runner-level "
                "unique-source reuse budget is preserved for execution."
            )
        ]
        missing_numeric_validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=missing_numeric_cap,
        )
        self.assertFalse(missing_numeric_validation["ok"], missing_numeric_validation)
        self.assertIn(
            "global_source_budget_not_preserved_in_constraints",
            {failure["code"] for failure in missing_numeric_validation["failures"]},
        )
        repaired_validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=repaired,
        )
        self.assertTrue(repaired_validation["ok"], repaired_validation)

    def test_request_source_budget_materialized_without_stale_constraints(self) -> None:
        question = "Compare official agency permit evidence across Korea municipalities"
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
        )
        candidate = response["candidate_plan"]
        candidate["constraints"] = []
        candidate["runner_source_budget"] = {
            "budget_cap_max_sources": 20,
            "materialized_from_budget_cap": True,
            "debug_note": "preserved from budget_cap before sanitization",
        }
        for task in candidate["bounded_tasks"]:
            task["max_sources"] = 3

        repaired, materializations = _materialize_candidate_budget_caps(
            candidate,
            budget_cap={"max_results": 8, "max_sources": 20},
        )

        budget_materialization = next(
            materialization
            for materialization in materializations
            if materialization.get("field") == "runner_source_budget"
        )
        self.assertEqual(
            budget_materialization["materialization"],
            "preserved_request_source_budget",
        )
        self.assertEqual(repaired["runner_source_budget"]["max_unique_sources"], 20)
        self.assertEqual(repaired["runner_source_budget"]["task_max_sources_sum"], 60)
        self.assertTrue(repaired["runner_source_budget"]["reuse_required"])
        constraints_text = json.dumps(repaired["constraints"], ensure_ascii=False)
        self.assertIn("max_unique_sources=20", constraints_text)
        self.assertNotIn("budget_cap", constraints_text.lower())
        runner_budget_text = json.dumps(
            repaired["runner_source_budget"],
            ensure_ascii=False,
            sort_keys=True,
        ).lower()
        self.assertNotIn("budget_cap", runner_budget_text)
        self.assertEqual(repaired["runner_source_budget"]["request_max_sources"], 20)
        self.assertTrue(
            repaired["runner_source_budget"]["materialized_from_request_source_limit"]
        )
        self.assertNotIn("debug_note", repaired["runner_source_budget"])
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=repaired,
        )
        self.assertTrue(validation["ok"], validation)

        missing_constraint = json.loads(json.dumps(repaired))
        missing_constraint.pop("constraints")
        missing_constraint_validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=missing_constraint,
        )
        self.assertFalse(
            missing_constraint_validation["ok"],
            missing_constraint_validation,
        )
        self.assertIn(
            "global_source_budget_not_preserved_in_constraints",
            {
                failure["code"]
                for failure in missing_constraint_validation["failures"]
            },
        )

    def test_request_source_budget_materialization_passes_substitute_check(self) -> None:
        question = "Compare official agency permit evidence across Korea municipalities"
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
        )
        candidate = response["candidate_plan"]
        candidate["constraints"] = []
        for task in candidate["bounded_tasks"]:
            task["max_sources"] = 3

        repaired, _materializations = _materialize_candidate_budget_caps(
            candidate,
            budget_cap={"max_results": 8, "max_sources": 20},
        )
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=repaired,
        )
        self.assertTrue(validation["ok"], validation)
        plan = self.semantic_plan_from_candidate_payload(repaired)
        plan_text = json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True).lower()
        self.assertIn("max_unique_sources", plan_text)
        self.assertNotIn("budget_cap", plan_text)

        oracle = self.semantic_internal_leakage_oracle()
        substitute = _semantic_substitute_implementation_check(
            plan=plan,
            oracle=oracle,
        )

        self.assertFalse(_has_forbidden_internal_leakage(plan=plan, oracle=oracle))
        self.assertTrue(substitute["passed"], substitute)
        self.assertEqual(
            substitute["forbidden_internal_implementation_terms_found"],
            [],
        )

    def test_multi_vendor_official_tasks_raise_source_cap_without_multiple_source_types(self) -> None:
        candidate = {
            "bounded_tasks": [
                {
                    "task_id": "task_edge_safari",
                    "query": (
                        "Edge+Safari official browser documentation parity notes "
                        "for storage behavior"
                    ),
                    "route": "text_only",
                    "freshness_requirement": "any",
                    "source_policy": {
                        "decision": "allowed",
                        "required_source_quality": [
                            "official browser vendor documentation"
                        ],
                    },
                    "expected_source_types": ["official vendor documentation"],
                    "expected_visual_targets": [],
                    "expected_artifacts": ["vendor documentation notes"],
                    "success_criteria": [
                        "Use official docs from each named browser vendor."
                    ],
                    "max_sources": 1,
                    "max_images": 0,
                    "done_condition": (
                        "Stop after official source-backed notes are recorded."
                    ),
                }
            ]
        }

        normalized, repairs = _normalize_candidate_executable_source_caps(candidate)

        self.assertEqual(normalized["bounded_tasks"][0]["max_sources"], 2)
        self.assertEqual(repairs[0]["task_id"], "task_edge_safari")
        self.assertIn("multiple_named_source_entities", repairs[0]["reasons"])

    def test_placeholder_municipalities_require_and_materialize_selection_workflow(self) -> None:
        question = "Compare public health permit evidence across four Korea municipalities"
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
            requirement_types=("subject", "source_quality", "geography"),
        )
        candidate = response["candidate_plan"]
        for index, task in enumerate(candidate["bounded_tasks"], start=1):
            municipality_number = ((index - 1) % 4) + 1
            task["query"] = (
                f"{question}: collect official permit records for "
                f"municipality {municipality_number}."
            )
            task["success_criteria"] = [
                (
                    "Find official source evidence for "
                    f"municipality {municipality_number}."
                )
            ]
            task["done_condition"] = (
                f"Stop after municipality {municipality_number} evidence is recorded."
            )
        candidate["constraints"].append(
            "Compare municipality 1, municipality 2, municipality 3, and "
            "municipality 4 using official records."
        )

        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=candidate,
        )
        self.assertFalse(validation["ok"])
        failure_codes = {failure["code"] for failure in validation["failures"]}
        self.assertIn("unbound_jurisdiction_placeholders", failure_codes)
        self.assertIn("missing_placeholder_selection_workflow", failure_codes)

        workflow_only = json.loads(json.dumps(candidate))
        workflow_only["constraints"].append(
            "Select and name each municipality placeholder using explicit criteria before collection."
        )
        workflow_only_validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=workflow_only,
        )
        workflow_only_codes = {
            failure["code"] for failure in workflow_only_validation["failures"]
        }
        self.assertFalse(workflow_only_validation["ok"], workflow_only_validation)
        self.assertIn("unbound_jurisdiction_placeholders", workflow_only_codes)
        self.assertNotIn("missing_placeholder_selection_workflow", workflow_only_codes)

        repaired, materializations = (
            _materialize_candidate_placeholder_selection_workflow(candidate)
        )

        self.assertTrue(materializations)
        repaired_text = json.dumps(repaired, ensure_ascii=False).lower()
        self.assertIn("selection workflow", repaired_text)
        self.assertIn("bind placeholder jurisdictions", repaired_text)
        self.assertIn("placeholder_binding", repaired)
        self.assertEqual(
            sorted(repaired["placeholder_binding"].keys()),
            ["municipality 1", "municipality 2", "municipality 3", "municipality 4"],
        )
        bound_names = [
            binding["jurisdiction_name"]
            for binding in repaired["placeholder_binding"].values()
        ]
        self.assertEqual(
            bound_names,
            [
                "Seoul, South Korea",
                "Busan, South Korea",
                "Incheon, South Korea",
                "Daegu, South Korea",
            ],
        )
        self.assertFalse(any("municipality " in name.lower() for name in bound_names))
        self.assertEqual(
            materializations[0]["placeholder_binding"]["municipality 1"][
                "jurisdiction_name"
            ],
            "Seoul, South Korea",
        )
        repaired_validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=repaired,
        )
        self.assertTrue(repaired_validation["ok"], repaired_validation)

    def test_visual_task_image_demands_over_schema_cap_remain_validation_failures(self) -> None:
        candidate = {
            "bounded_tasks": [
                {
                    "task_id": "task_visual_cap",
                    "angle_id": "angle_visual",
                    "query": (
                        "Find downloadable public recycling posters and acquire "
                        "one representative image per program where available."
                    ),
                    "route": "visual_required",
                    "freshness_requirement": "any",
                    "source_policy": {"decision": "allowed", "flags": []},
                    "expected_source_types": ["official municipal poster page"],
                    "expected_visual_targets": ["poster pages"],
                    "expected_artifacts": ["poster manifest"],
                    "success_criteria": [
                        "At least four jurisdiction-linked poster examples are sought.",
                        "Every acquired image is provenance-verified.",
                    ],
                    "max_sources": 2,
                    "max_images": 3,
                    "done_condition": (
                        "Done when at least four images are acquired for the sampled programs."
                    ),
                }
            ]
        }

        repaired, materializations = _materialize_candidate_visual_image_cap_feasibility(
            candidate
        )

        self.assertFalse(materializations)
        task = repaired["bounded_tasks"][0]
        text = json.dumps(task, ensure_ascii=False)
        self.assertIn("At least four", text)
        self.assertIn("one representative image per program", text)
        self.assertIn("at least four images", text)
        self.assertNotIn("Up to 3", text)
        validation = validate_semantic_candidate_plan(
            original_question="Compare public recycling poster examples.",
            plan=repaired,
        )
        self.assertFalse(validation["ok"], validation)
        self.assertIn(
            "bounded_task_requirement_exceeds_max_images",
            {failure["code"] for failure in validation["failures"]},
        )

    def test_per_entity_visual_image_demands_count_against_max_images(self) -> None:
        candidate = {
            "bounded_tasks": [
                {
                    "task_id": "task_program_posters",
                    "angle_id": "angle_visual",
                    "query": (
                        "Compare four public recycling programs and collect one "
                        "representative poster image per program where available."
                    ),
                    "route": "visual_required",
                    "freshness_requirement": "any",
                    "source_policy": {"decision": "allowed", "flags": []},
                    "expected_source_types": ["official municipal program page"],
                    "expected_visual_targets": [
                        "one representative poster image per program"
                    ],
                    "expected_artifacts": ["program poster evidence table"],
                    "success_criteria": [
                        "Cover four programs with provenance for each poster example.",
                        "Every acquired image is linked to its program source.",
                    ],
                    "max_sources": 3,
                    "max_images": 3,
                    "done_condition": (
                        "Done when one representative poster image per program is "
                        "verified across four programs with source notes."
                    ),
                }
            ]
        }

        repaired, materializations = _materialize_candidate_visual_image_cap_feasibility(
            candidate
        )
        task_text = json.dumps(repaired["bounded_tasks"][0], ensure_ascii=False).lower()

        self.assertFalse(materializations)
        self.assertNotIn("four images", task_text)
        validation = validate_semantic_candidate_plan(
            original_question="Compare public recycling program poster examples.",
            plan=repaired,
        )
        self.assertFalse(validation["ok"], validation)
        failure = next(
            failure
            for failure in validation["failures"]
            if failure["code"] == "bounded_task_requirement_exceeds_max_images"
        )
        self.assertEqual(failure["required_count"], 4)
        self.assertEqual(failure["max_images"], 3)
        self.assertEqual(
            failure["explicit_requirement_counts"]["image_per_program"],
            4,
        )

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
        self.assertNotIn("Budget cap", constraint_text)
        self.assertIn("candidate_plan_request_budget_materializations", raw_response)
        self.assertNotIn("candidate_plan_budget_cap_materializations", raw_response)
        semantic_plan_text = json.dumps(
            semantic_plan,
            ensure_ascii=False,
            sort_keys=True,
        ).lower()
        self.assertIn("runner_source_budget", semantic_plan_text)
        self.assertIn("max_unique_sources", semantic_plan_text)
        self.assertNotIn("budget_cap", semantic_plan_text)
        reviewer_plan_text = json.dumps(
            adapter_request["_reviewer_requests"][-1]["semantic_plan"],
            ensure_ascii=False,
            sort_keys=True,
        ).lower()
        self.assertIn("runner_source_budget", reviewer_plan_text)
        self.assertIn("max_unique_sources", reviewer_plan_text)
        self.assertNotIn("budget_cap", reviewer_plan_text)
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

        text_only_visual_expected = json.loads(json.dumps(plan))
        text_task = next(
            task
            for task in text_only_visual_expected["bounded_tasks"]
            if task["route"] == "text_only"
        )
        text_task["expected_evidence"] = ["visual_example", "vlm_analysis"]
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=text_only_visual_expected,
        )
        self.assertFalse(validation["ok"])
        self.assertIn(
            "text_only_task_visual_expected_evidence",
            {failure["code"] for failure in validation["failures"]},
        )

        falsy_route_visual_expected = json.loads(json.dumps(plan))
        text_task = next(
            task
            for task in falsy_route_visual_expected["bounded_tasks"]
            if task["route"] == "text_only"
        )
        text_task["route"] = ""
        text_task["expected_evidence"] = ["visual_example"]
        validation = validate_semantic_candidate_plan(
            original_question=question,
            plan=falsy_route_visual_expected,
        )
        self.assertFalse(validation["ok"])
        self.assertIn(
            "text_only_task_visual_expected_evidence",
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
