from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "plugins" / "codex-deepresearch" / "scripts" / "codex-deepresearch"
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch import (  # noqa: E402
    CodexExecAdapter,
    FixtureAdapter,
    ParallelOrchestrationError,
    inspect_run_state,
    merge_evidence_shards,
    plan_research_tasks,
    prepare_run,
    read_trace_records,
    run_parallel_orchestration,
    validate_artifacts,
    validate_trace_file,
)


class ParallelOrchestratorTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def load_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def prepare(self, *, route: str = "text_only", budget: str = "standard") -> Path:
        prepared = prepare_run(
            question="Research a broad private alpha launch with competitors, policies, visuals, and pricing.",
            runs_dir=self.temp_runs_dir(),
            route=route,
            budget_preset=budget,
        )
        return Path(prepared["run_dir"])

    def shard(
        self,
        run_dir: Path,
        task: dict,
        *,
        duplicate: bool = False,
        common_ids: bool = False,
    ) -> dict:
        evidence = self.load_json(run_dir / "evidence.json")
        source_id = "src_001" if common_ids else f"src_{task['id']}"
        image_id = "img_001" if common_ids else f"img_{task['id']}"
        claim_id = "claim_001" if common_ids else f"claim_{task['id']}"
        url = "https://example.com/duplicate" if duplicate else f"https://example.com/{task['id']}"
        claim_text = (
            "The duplicate claim text is equivalent."
            if duplicate
            else f"Unique claim for {task['id']}."
        )
        return {
            "schema_version": "0.1.0",
            "run_id": f"{evidence['run_id']}-{task['id']}",
            "created_at": "2026-06-23T00:00:00Z",
            "question": evidence["question"],
            "mode": evidence["mode"],
            "search_provider": evidence["search_provider"],
            "vlm_provider": evidence["vlm_provider"],
            "sources": [
                {
                    "id": source_id,
                    "type": "web",
                    "url": url,
                    "title": "Shard source",
                    "published_at": None,
                    "accessed_at": "2026-06-23T00:00:00Z",
                    "quality": "primary",
                    "retrieval_status": "fetched",
                    "local_artifact_path": f"evidence_shards/{task['id']}/source.html",
                    "license_policy": "allowed",
                    "robots_policy": "allowed",
                    "policy_decision": "allowed",
                    "policy_flags": [],
                }
            ],
            "images": [
                {
                    "id": image_id,
                    "source_id": source_id,
                    "origin": "screenshot",
                    "page_url": url,
                    "image_url": url + "/image.png",
                    "local_artifact_path": f"evidence_shards/{task['id']}/image.png",
                    "mime_type": "image/png",
                    "width": 640,
                    "height": 360,
                    "observations": ["A visible fixture image."],
                    "inferences": [],
                    "visual_tasks": [task["id"]],
                    "analysis_provider": evidence["vlm_provider"],
                    "analysis_status": "analyzed",
                    "policy_flags": [],
                    "caveats": [],
                    "content_hash": "duplicate-image-hash" if duplicate else f"hash-{task['id']}",
                }
            ],
            "claims": [
                {
                    "id": claim_id,
                    "text": claim_text,
                    "claim_type": "mixed",
                    "supporting_sources": [source_id],
                    "supporting_images": [image_id],
                    "quote_spans": [
                        {
                            "source_id": source_id,
                            "quote": claim_text,
                            "location": "paragraph 1",
                        }
                    ],
                    "votes": [],
                    "verification_status": "supported",
                    "review_status": "human_accepted",
                    "promotion_status": "not_eligible",
                    "confidence": "medium",
                    "caveats": [],
                }
            ],
        }

    def test_planner_output_expands_to_twenty_bounded_research_tasks(self) -> None:
        run_dir = self.prepare()

        result = plan_research_tasks(run=run_dir, min_tasks=20)

        self.assertEqual(result["task_count"], 20)
        tasks = self.load_json(run_dir / "research_tasks.json")
        self.assertEqual(len(tasks["tasks"]), 20)
        self.assertEqual(tasks["max_concurrent_codex_subagents"], 8)
        self.assertTrue(all(task["state"] == "queued" for task in tasks["tasks"]))
        self.assertTrue(all(task["output_shard_path"].startswith("evidence_shards/") for task in tasks["tasks"]))

    def test_codex_exec_adapter_builds_required_json_command(self) -> None:
        run_dir = self.prepare()
        task = plan_research_tasks(run=run_dir, min_tasks=1)["tasks"][0]

        command = CodexExecAdapter(project_root=ROOT).build_command(task, max_threads=8, run_dir=run_dir)

        self.assertEqual(command[:3], ["codex", "exec", "--json"])
        self.assertIn("-C", command)
        self.assertEqual(command[command.index("-C") + 1], str(ROOT))
        self.assertIn("--add-dir", command)
        self.assertEqual(command[command.index("--add-dir") + 1], str(run_dir.resolve()))
        self.assertNotIn("--skip-git-repo-check", command)
        self.assertIn("agents.max_threads=8", command)
        self.assertIn("sandbox_mode=workspace-write", command)
        self.assertIn("approval_policy=never", command)
        self.assertIn(str(run_dir / "evidence_shards/task_research_001/evidence_shard.json"), command[-1])
        shard_dir = run_dir / "evidence_shards/task_research_001"
        self.assertIn(str(shard_dir / "search_results.jsonl"), command[-1])
        self.assertIn(str(shard_dir / "visual_observations.jsonl"), command[-1])
        self.assertIn(str(shard_dir / "verifier_votes.jsonl"), command[-1])
        self.assertIn(f"Do not write sidecars outside {shard_dir}", command[-1])

    def test_invalid_output_shard_paths_are_rejected_before_child_execution(self) -> None:
        run_dir = self.prepare()
        base_task = plan_research_tasks(run=run_dir, min_tasks=1)["tasks"][0]
        adapter = CodexExecAdapter(project_root=ROOT, timeout_seconds=12)

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator.subprocess.run") as run_mock,
        ):
            for raw_path in ("/tmp/escape/evidence_shard.json", "../escape/evidence_shard.json"):
                task = dict(base_task)
                task["output_shard_path"] = raw_path

                result = adapter.run_task(task, run_dir=run_dir, max_threads=3)

                self.assertEqual(result.status, "failed")
                self.assertEqual(result.failure_category, "missing_shard")
                self.assertIsNotNone(result.message)
                assert result.message is not None
                self.assertIn("invalid output_shard_path", result.message)
                self.assertIn(raw_path, result.message)
                self.assertIn("must be relative and stay under run_dir", result.message)
                self.assertEqual(result.events[-1]["raw_event"]["output_shard_path"], raw_path)

            run_mock.assert_not_called()

    def test_merge_discards_escaped_output_shard_path_without_reading_outside_run_dir(self) -> None:
        run_dir = self.prepare()
        plan_research_tasks(run=run_dir, min_tasks=1)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        task = tasks_artifact["tasks"][0]
        task["state"] = "completed"
        task["output_shard_path"] = "../outside/evidence_shard.json"
        outside_shard = run_dir.parent / "outside" / "evidence_shard.json"
        outside_shard.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(outside_shard, self.shard(run_dir, task))
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)

        merge = merge_evidence_shards(run=run_dir)

        self.assertEqual(merge["accepted_shards"], [])
        self.assertEqual(merge["rejected_shards"][0]["reason"], "invalid_output_shard_path")
        self.assertIn("must be relative and stay under run_dir", merge["rejected_shards"][0]["diagnostic"])
        tasks = self.load_json(run_dir / "research_tasks.json")["tasks"]
        self.assertEqual(tasks[0]["state"], "discarded")
        self.assertEqual(tasks[0]["discard_reason"], "invalid_output_shard_path")
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(evidence["claims"], [])

    def test_codex_exec_adapter_runs_from_project_root_and_reports_trust_errors(self) -> None:
        run_dir = self.prepare()
        task = plan_research_tasks(run=run_dir, min_tasks=1)["tasks"][0]
        stderr = "Not inside a trusted directory and --skip-git-repo-check was not specified."
        adapter = CodexExecAdapter(project_root=ROOT, timeout_seconds=12)

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator.subprocess.run") as run_mock,
        ):
            run_mock.return_value = subprocess.CompletedProcess(
                args=["codex"],
                returncode=1,
                stdout='{"type":"message","status":"error","message":"trust failed"}\n',
                stderr=stderr,
            )

            result = adapter.run_task(task, run_dir=run_dir, max_threads=3)

        command = run_mock.call_args.args[0]
        kwargs = run_mock.call_args.kwargs
        self.assertEqual(kwargs["cwd"], ROOT)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(command[command.index("-C") + 1], str(ROOT))
        self.assertEqual(command[command.index("--add-dir") + 1], str(run_dir.resolve()))
        self.assertNotIn("--skip-git-repo-check", command)
        self.assertIn(str(run_dir / task["output_shard_path"]), command[-1])
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure_category, "codex_exec_failed")
        self.assertIsNotNone(result.message)
        assert result.message is not None
        self.assertIn(stderr, result.message)
        self.assertIn(f"cwd={ROOT}", result.message)
        self.assertIn(f"run_dir={run_dir.resolve()}", result.message)
        self.assertIn("repo_check_bypass_used=False", result.message)
        self.assertIn("do not count --skip-git-repo-check bypass runs", result.message)
        spawn_context = result.events[0]["raw_event"]
        self.assertEqual(spawn_context["trusted_project_root"], str(ROOT))
        self.assertEqual(spawn_context["run_dir"], str(run_dir.resolve()))
        self.assertFalse(spawn_context["repo_check_bypass_used"])

    def test_codex_exec_timeout_after_valid_shard_keeps_completed_output(self) -> None:
        run_dir = self.prepare()
        task = plan_research_tasks(run=run_dir, min_tasks=1)["tasks"][0]
        shard_path = run_dir / task["output_shard_path"]
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(shard_path, self.shard(run_dir, task))
        adapter = CodexExecAdapter(project_root=ROOT, timeout_seconds=12)

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator.subprocess.run") as run_mock,
        ):
            run_mock.side_effect = subprocess.TimeoutExpired(
                cmd="codex",
                timeout=12,
                output='{"type":"thread.started"}\n',
                stderr="Auth(AuthorizationRequired)",
            )

            result = adapter.run_task(task, run_dir=run_dir, max_threads=3)

        self.assertEqual(run_mock.call_args.kwargs["cwd"], ROOT)
        self.assertEqual(run_mock.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(result.status, "completed")
        self.assertIsNone(result.failure_category)
        self.assertEqual(result.shard_path, str(shard_path))
        wait_events = [event for event in result.events if event["event_type"] == "wait"]
        self.assertEqual(wait_events[-1]["child_status"], "completed")
        self.assertIn("timed out after writing a valid shard", wait_events[-1]["child_message"])
        self.assertTrue(wait_events[-1]["raw_event"]["timeout_after_valid_shard"])

    def test_fixture_runner_records_codex_events_and_schema_valid_shards(self) -> None:
        run_dir = self.prepare(route="visual_optional")

        result = run_parallel_orchestration(run=run_dir, adapter_name="fixture", min_tasks=3)

        self.assertFalse(result["parallel_degraded"])
        self.assertEqual(result["adapter"], "fixture")
        self.assertEqual(result["evidence_source"]["type"], "fixture")
        self.assertTrue(result["evidence_source"]["fixture_only"])
        self.assertFalse(result["evidence_source"]["real_child_execution"])
        self.assertFalse(result["evidence_source"]["real_use_e2e_eligible"])
        self.assertEqual(result["evidence_source"]["accepted_shards"], 3)
        self.assertEqual(result["merge"]["evidence_source"]["type"], "fixture")
        self.assertEqual(result["max_scheduled_concurrency"], 8)
        self.assertTrue((run_dir / "subagent_assignments.jsonl").is_file())
        tasks = self.load_json(run_dir / "research_tasks.json")["tasks"]
        self.assertTrue(all(task["state"] == "merged" for task in tasks))
        for task in tasks:
            validation = validate_artifacts(evidence_path=run_dir / task["output_shard_path"])
            self.assertTrue(validation.valid, validation.to_dict())

        records = read_trace_records(run_dir / "run_trace.jsonl")
        event_types = [record.get("event_type") for record in records]
        self.assertIn("spawn_agent", event_types)
        self.assertIn("wait", event_types)
        self.assertIn("close_agent", event_types)
        self.assertTrue(any(record.get("child_thread_id") for record in records))
        self.assertTrue(validate_trace_file(run_dir / "run_trace.jsonl").valid)

        evidence_validation = validate_artifacts(evidence_path=run_dir / "evidence.json")
        self.assertTrue(evidence_validation.valid, evidence_validation.to_dict())
        state = inspect_run_state(run_dir)
        stages = {stage["stage"]: stage for stage in state["stages"]}
        self.assertEqual(stages["parallel_orchestration"]["status"], "completed")
        self.assertEqual(
            stages["parallel_orchestration"]["evidence_source"]["type"],
            "fixture",
        )
        self.assertEqual(stages["ingest"]["status"], "skipped")
        self.assertEqual(stages["fetch_claims"]["status"], "skipped")
        self.assertEqual(stages["ingest_vision"]["status"], "skipped")
        self.assertEqual(state["next_safe_stage"], "enforce_guardrails")

    def test_shard_merge_deduplicates_sources_images_and_claim_text(self) -> None:
        run_dir = self.prepare()
        plan_research_tasks(run=run_dir, min_tasks=2)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        for task in tasks_artifact["tasks"]:
            task["state"] = "completed"
            shard_path = run_dir / task["output_shard_path"]
            shard_path.parent.mkdir(parents=True, exist_ok=True)
            self.write_json(shard_path, self.shard(run_dir, task, duplicate=True))
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)

        merge = merge_evidence_shards(run=run_dir)

        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(len(evidence["sources"]), 1)
        self.assertEqual(len(evidence["images"]), 1)
        self.assertEqual(len(evidence["claims"]), 1)
        self.assertTrue(merge["source_dedupe"])
        self.assertTrue(merge["image_dedupe"])
        self.assertTrue(merge["claim_dedupe"])
        self.assertTrue(validate_artifacts(evidence_path=run_dir / "evidence.json").valid)

    def test_shard_merge_namespaces_colliding_local_ids_and_remaps_refs(self) -> None:
        run_dir = self.prepare()
        plan_research_tasks(run=run_dir, min_tasks=2)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        for task in tasks_artifact["tasks"]:
            task["state"] = "completed"
            shard_path = run_dir / task["output_shard_path"]
            shard_path.parent.mkdir(parents=True, exist_ok=True)
            self.write_json(shard_path, self.shard(run_dir, task, common_ids=True))
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)

        merge = merge_evidence_shards(run=run_dir)

        self.assertEqual(merge["status"], "completed", merge)
        evidence = self.load_json(run_dir / "evidence.json")
        source_ids = [source["id"] for source in evidence["sources"]]
        image_ids = [image["id"] for image in evidence["images"]]
        claim_ids = [claim["id"] for claim in evidence["claims"]]
        self.assertEqual(len(source_ids), len(set(source_ids)))
        self.assertEqual(len(image_ids), len(set(image_ids)))
        self.assertEqual(len(claim_ids), len(set(claim_ids)))
        self.assertEqual(len(evidence["sources"]), 2)
        self.assertEqual(len(evidence["claims"]), 2)
        for claim in evidence["claims"]:
            self.assertIn(claim["supporting_sources"][0], source_ids)
            self.assertEqual(claim["quote_spans"][0]["source_id"], claim["supporting_sources"][0])
            self.assertIn(claim["supporting_images"][0], image_ids)
        self.assertTrue(validate_artifacts(evidence_path=run_dir / "evidence.json").valid)

    def test_completed_task_requires_schema_valid_shard(self) -> None:
        run_dir = self.prepare()
        plan_research_tasks(run=run_dir, min_tasks=1)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        task = tasks_artifact["tasks"][0]
        shard_path = run_dir / task["output_shard_path"]
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(shard_path, {"schema_version": "0.1.0", "sources": []})

        class InvalidShardAdapter:
            name = "invalid-shard"

            def run_task(self, task, *, run_dir, max_threads):
                return type("Result", (), {
                    "task_id": task["id"],
                    "status": "completed",
                    "child_thread_id": "invalid-shard-thread",
                    "events": (),
                    "shard_path": str(run_dir / task["output_shard_path"]),
                    "failure_category": None,
                    "message": None,
                })()

        with mock.patch("deepresearch.parallel_orchestrator._adapter", return_value=InvalidShardAdapter()):
            run_parallel_orchestration(run=run_dir, adapter_name="fixture", min_tasks=1)

        task = self.load_json(run_dir / "research_tasks.json")["tasks"][0]
        self.assertEqual(task["state"], "retryable")
        self.assertEqual(task["failure_category"], "invalid_shard")

    def test_fixture_adapter_uses_bounded_parallel_scheduler(self) -> None:
        run_dir = self.prepare()
        lock = threading.Lock()
        in_flight = 0
        max_seen = 0

        class InstrumentedFixture(FixtureAdapter):
            name = "instrumented-fixture"

            def run_task(self, task, *, run_dir, max_threads):
                nonlocal in_flight, max_seen
                with lock:
                    in_flight += 1
                    max_seen = max(max_seen, in_flight)
                try:
                    time.sleep(0.03)
                    return super().run_task(task, run_dir=run_dir, max_threads=max_threads)
                finally:
                    with lock:
                        in_flight -= 1

        with mock.patch("deepresearch.parallel_orchestrator._adapter", return_value=InstrumentedFixture()):
            result = run_parallel_orchestration(run=run_dir, adapter_name="fixture", min_tasks=6)

        self.assertEqual(result["max_scheduled_concurrency"], 8)
        self.assertGreater(max_seen, 1)

    def test_retry_safe_failed_task_does_not_rerun_completed_tasks(self) -> None:
        run_dir = self.prepare()
        plan_research_tasks(run=run_dir, min_tasks=2)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        tasks_artifact["tasks"][0]["state"] = "completed"
        first_shard_path = run_dir / tasks_artifact["tasks"][0]["output_shard_path"]
        first_shard_path.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(first_shard_path, self.shard(run_dir, tasks_artifact["tasks"][0]))
        tasks_artifact["tasks"][1]["state"] = "failed"
        tasks_artifact["tasks"][1]["failure_category"] = "missing_shard"
        tasks_artifact["tasks"][1]["attempt"] = 1
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)
        before = 0

        run_parallel_orchestration(
            run=run_dir,
            adapter_name="fixture",
            min_tasks=2,
            retry_failed=True,
        )

        after = len((run_dir / "subagent_assignments.jsonl").read_text(encoding="utf-8").splitlines())
        self.assertEqual(after, before + 1)
        tasks = self.load_json(run_dir / "research_tasks.json")["tasks"]
        self.assertEqual([task["state"] for task in tasks], ["merged", "merged"])

    def test_blocked_and_discarded_tasks_are_preserved_in_merge_status(self) -> None:
        run_dir = self.prepare()
        plan_research_tasks(run=run_dir, min_tasks=3)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        tasks_artifact["tasks"][0]["state"] = "blocked"
        tasks_artifact["tasks"][0]["blocked_reason"] = "policy_blocked"
        tasks_artifact["tasks"][1]["state"] = "discarded"
        tasks_artifact["tasks"][1]["discard_reason"] = "budget_pruned"
        tasks_artifact["tasks"][2]["state"] = "completed"
        shard_path = run_dir / tasks_artifact["tasks"][2]["output_shard_path"]
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(shard_path, self.shard(run_dir, tasks_artifact["tasks"][2]))
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)

        merge = merge_evidence_shards(run=run_dir)

        self.assertEqual(merge["blocked_tasks"][0]["reason"], "policy_blocked")
        self.assertEqual(merge["discarded_tasks"][0]["reason"], "budget_pruned")
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(len(evidence["claims"]), 1)
        self.assertNotIn("policy_blocked", json.dumps(evidence["claims"]))

    def test_codex_exec_unavailable_records_degraded_serial_execution(self) -> None:
        run_dir = self.prepare()

        with mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value=None):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                min_tasks=2,
            )

        self.assertTrue(result["parallel_degraded"])
        self.assertEqual(result["degraded_reason"], "codex_exec_unavailable")
        self.assertEqual(result["adapter"], "serial-degraded")
        self.assertEqual(result["evidence_source"]["type"], "serial_handoff")
        self.assertFalse(result["evidence_source"]["fixture_only"])
        self.assertFalse(result["evidence_source"]["real_child_execution"])
        self.assertFalse(result["evidence_source"]["real_use_e2e_eligible"])
        self.assertEqual(result["max_scheduled_concurrency"], 1)
        self.assertEqual(result["status"], "degraded_serial_handoff_required")
        self.assertTrue(result["needs_serial_handoff"])
        tasks = self.load_json(run_dir / "research_tasks.json")
        self.assertTrue(tasks["parallel_degraded"])
        self.assertTrue(all(task["state"] == "blocked" for task in tasks["tasks"]))
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(evidence["claims"], [])
        merge = self.load_json(run_dir / "merge_status.json")
        self.assertEqual(merge["evidence_source"]["type"], "serial_handoff")
        self.assertEqual(len(merge["blocked_tasks"]), 2)
        self.assertEqual(merge["accepted_shards"], [])
        state = inspect_run_state(run_dir)
        stages = {stage["stage"]: stage for stage in state["stages"]}
        self.assertEqual(stages["parallel_orchestration"]["status"], "completed")
        self.assertEqual(stages["ingest"]["status"], "pending")
        self.assertEqual(state["next_safe_stage"], "ingest")

    def test_exhaustive_plan_requires_confirmation_and_cost_cap(self) -> None:
        run_dir = self.prepare()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["budget"]["preset"] = "exhaustive"
        evidence["budget"].pop("max_cost_usd", None)
        self.write_json(run_dir / "evidence.json", evidence)
        (run_dir / "budget_estimate.json").unlink()

        with self.assertRaises(ParallelOrchestrationError):
            plan_research_tasks(run=run_dir, min_tasks=100)
        with self.assertRaises(ParallelOrchestrationError):
            plan_research_tasks(run=run_dir, min_tasks=100, confirm_exhaustive=True)

        result = plan_research_tasks(
            run=run_dir,
            min_tasks=100,
            confirm_exhaustive=True,
            max_cost_usd=1.0,
        )
        self.assertEqual(result["task_count"], 100)

    def test_cli_m18_fixture_smoke(self) -> None:
        runs_dir = self.temp_runs_dir()
        prepare = subprocess.run(
            [
                str(RUNNER),
                "prepare",
                "M18 CLI fixture smoke",
                "--runs-dir",
                str(runs_dir),
                "--route",
                "text_only",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(prepare.returncode, 0, prepare.stderr)
        run_dir = Path(json.loads(prepare.stdout)["run_dir"])

        smoke = subprocess.run(
            [
                str(RUNNER),
                "orchestrate-parallel",
                "--run",
                str(run_dir),
                "--adapter",
                "fixture",
                "--min-tasks",
                "3",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(smoke.returncode, 0, smoke.stderr)
        payload = json.loads(smoke.stdout)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["adapter"], "fixture")
        self.assertEqual(payload["evidence_source"]["type"], "fixture")
        self.assertTrue(payload["evidence_source"]["fixture_only"])
        self.assertFalse(payload["evidence_source"]["real_use_e2e_eligible"])
        self.assertTrue((run_dir / "merge_status.json").is_file())


if __name__ == "__main__":
    unittest.main()
