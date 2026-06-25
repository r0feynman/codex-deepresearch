from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "plugins" / "codex-deepresearch" / "scripts" / "codex-deepresearch"
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch.run_monitor import (  # noqa: E402
    inspect_run_monitor,
    list_run_monitors,
    render_run_detail,
    render_run_list,
)
from deepresearch.run_state import cancel_run, pause_run  # noqa: E402


class RunMonitorTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def write_jsonl(self, path: Path, records: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )

    def make_run(self, runs_dir: Path, run_id: str = "run-fixture-001") -> Path:
        run_dir = runs_dir / run_id
        run_dir.mkdir(parents=True)
        self.write_json(
            run_dir / "evidence.json",
            {
                "run_id": run_id,
                "search_provider": "codex-native",
                "budget": {
                    "preset": "deep",
                    "max_concurrent_codex_subagents": 24,
                    "max_cost_usd": 25.0,
                },
                "sources": [{"id": "src_001"}, {"id": "src_002"}],
                "images": [{"id": "img_001"}],
                "claims": [{"id": "claim_001"}, {"id": "claim_002"}],
            },
        )
        return run_dir

    def test_interrupted_controls_are_visible_in_list_and_detail(self) -> None:
        runs_dir = self.temp_runs_dir()
        paused_dir = self.make_run(runs_dir, "run-paused-001")
        pause_run(
            paused_dir,
            reason="monitor_pause",
            timestamp="2026-06-25T00:00:00Z",
        )

        paused_detail = inspect_run_monitor(paused_dir)
        paused_list = list_run_monitors(runs_dir)
        rendered_paused_detail = render_run_detail(paused_detail)
        rendered_paused_list = render_run_list(paused_list)

        self.assertEqual(paused_detail["status"], "paused")
        self.assertFalse(paused_detail["terminal"])
        self.assertEqual(paused_detail["phase"]["stage"], "paused")
        self.assertEqual(paused_detail["control"]["status"], "paused")
        self.assertIn("Control: status=paused", rendered_paused_detail)
        self.assertIn("paused", rendered_paused_list)

        cancelled_dir = self.make_run(runs_dir, "run-cancelled-001")
        self.write_json(
            cancelled_dir / "research_tasks.json",
            {
                "run_id": "run-cancelled-001",
                "task_count": 1,
                "tasks": [
                    {
                        "id": "task_001",
                        "state": "running",
                        "last_child_thread_id": "codex-task_001-attempt-1",
                    }
                ],
            },
        )
        self.write_jsonl(
            cancelled_dir / "subagent_assignments.jsonl",
            [
                {
                    "task_id": "task_001",
                    "state": "assigned",
                    "child_thread_id": "codex-task_001-attempt-1",
                    "timestamp": "2026-06-25T00:00:05Z",
                }
            ],
        )
        cancel_run(
            cancelled_dir,
            reason="monitor_cancel",
            timestamp="2026-06-25T00:01:00Z",
        )

        cancelled_detail = inspect_run_monitor(cancelled_dir)
        rendered_cancelled_detail = render_run_detail(cancelled_detail)
        rendered_cancelled_list = render_run_list(list_run_monitors(runs_dir))

        self.assertEqual(cancelled_detail["status"], "cancelled")
        self.assertTrue(cancelled_detail["terminal"])
        self.assertEqual(cancelled_detail["phase"]["stage"], "cancelled")
        self.assertEqual(cancelled_detail["control"]["status"], "cancelled")
        self.assertEqual(cancelled_detail["control"]["child_close_records"], 1)
        self.assertIn("Control: status=cancelled", rendered_cancelled_detail)
        self.assertIn("cancelled", rendered_cancelled_list)

    def test_detail_classifies_shards_serial_fallback_budget_and_paths(self) -> None:
        runs_dir = self.temp_runs_dir()
        run_dir = self.make_run(runs_dir, "run-serial-001")
        outside_private_path = "/outside-run-dir/private/session.json"
        relative_private_path = "../private/session.json"
        self.write_json(
            run_dir / "run_status.json",
            {
                "run_id": "run-serial-001",
                "run_dir": str(run_dir),
                "selected_mode": "full-runner",
                "status": "degraded_serial_handoff_required",
                "ok": True,
                "terminal": True,
                "provenance": {
                    "type": "serial_handoff",
                    "adapter": "serial-degraded",
                },
                "fallback": {
                    "parallel_degraded": False,
                    "needs_serial_handoff": True,
                },
                "artifacts": {
                    "run_status": str(run_dir / "run_status.json"),
                    "private_debug": outside_private_path,
                    "relative_private_debug": relative_private_path,
                },
            },
        )
        self.write_json(
            run_dir / "run_steps.json",
            {
                "run_id": "run-serial-001",
                "status": "in_progress",
                "next_safe_stage": "parallel_orchestration",
                "next_stage_retryable": False,
                "stages": {
                    "planning": {"stage": "planning", "status": "completed"},
                    "parallel_orchestration": {
                        "stage": "parallel_orchestration",
                        "status": "running",
                    },
                },
            },
        )
        self.write_json(
            run_dir / "research_tasks.json",
            {
                "run_id": "run-serial-001",
                "task_count": 7,
                "tasks": [
                    {"id": "task_queued", "state": "queued", "attempt": 0},
                    {"id": "task_assigned", "state": "assigned", "attempt": 1},
                    {"id": "task_running", "state": "running", "attempt": 1},
                    {"id": "task_failed", "state": "failed", "attempt": 1},
                    {"id": "task_retry", "state": "retryable", "attempt": 1},
                    {"id": "task_blocked", "state": "blocked", "attempt": 1},
                    {"id": "task_merged", "state": "merged", "attempt": 2},
                ],
            },
        )
        merge_status = {
            "run_id": "run-serial-001",
            "status": "completed",
            "parallel_degraded": False,
            "evidence_source": {"type": "serial_handoff", "adapter": "serial-degraded"},
            "accepted_shards": [
                {"task_id": "task_merged", "path": str(run_dir / "evidence_shards/task_merged/evidence_shard.json")}
            ],
            "failure_counts": {"failed_tasks": 1, "blocked_tasks": 1},
        }
        self.write_json(run_dir / "merge_status.json", merge_status)
        self.write_json(
            run_dir / "parallel_orchestration_status.json",
            {
                "run_id": "run-serial-001",
                "status": "degraded_serial_handoff_required",
                "ok": True,
                "parallel_degraded": False,
                "adapter": "serial-degraded",
                "planned_task_count": 7,
                "runnable_task_count": 7,
                "needs_serial_handoff": True,
                "merge": merge_status,
                "failure_counts": {"failed_tasks": 1, "blocked_tasks": 1},
            },
        )
        self.write_json(
            run_dir / "budget_estimate.json",
            {
                "budget_preset": "deep",
                "confirmation": {"required": True, "provided": True},
                "effective_caps": {
                    "max_concurrent_codex_subagents": 24,
                    "max_cost_usd": 25.0,
                },
                "estimates": {"codex_subagent_count": 7},
                "high_water_cost_bounds": {
                    "upper_bound_usd": 18.5,
                    "max_cost_usd": 25.0,
                    "within_max_cost": True,
                },
            },
        )
        self.write_json(
            run_dir / "visual_provider_status.json",
            {
                "status": "visual_candidates_collected",
                "providers": [{"provider": "local-page"}, {"provider": "browser-screenshot"}],
            },
        )
        self.write_json(
            run_dir / "visual_acquisition_status.json",
            {
                "status": "visual_candidates_collected",
                "candidate_records": 3,
                "selected_observations": 2,
            },
        )
        self.write_jsonl(
            run_dir / "image_fetch_status.jsonl",
            [{"fetch_status": "fetched"}, {"fetch_status": "failed"}],
        )
        self.write_jsonl(
            run_dir / "run_trace.jsonl",
            [{"stage": "parallel_orchestration", "status": "running", "event_type": "stage"}],
        )

        detail = inspect_run_monitor(run_dir)

        self.assertEqual(detail["phase"]["stage"], "parallel_orchestration")
        self.assertEqual(detail["phase"]["status"], "running")
        self.assertEqual(detail["mode"]["kind"], "serial_fallback")
        self.assertEqual(detail["shards"]["queued"], 1)
        self.assertEqual(detail["shards"]["active"], 2)
        self.assertEqual(detail["shards"]["failed"], 1)
        self.assertEqual(detail["shards"]["accepted"], 1)
        self.assertEqual(detail["shards"]["merged"], 1)
        self.assertEqual(detail["shards"]["retried"], 2)
        self.assertEqual(detail["shards"]["blocked"], 1)
        self.assertEqual(detail["budget"]["preset"], "deep")
        self.assertTrue(detail["budget"]["confirmation_required"])
        self.assertTrue(detail["budget"]["confirmation_provided"])
        self.assertEqual(detail["budget"]["max_concurrent_codex_subagents"], 24)
        self.assertEqual(detail["artifacts"]["private_debug"], "<outside-run-dir>")
        self.assertEqual(detail["artifacts"]["relative_private_debug"], "<outside-run-dir>")

        rendered = render_run_detail(detail)
        self.assertIn("serial fallback", rendered)
        self.assertIn("confirmation_provided=yes", rendered)
        self.assertNotIn(outside_private_path, rendered)
        self.assertNotIn(relative_private_path, rendered)

    def test_list_monitor_keeps_paths_run_relative_and_labels_fixture(self) -> None:
        runs_dir = self.temp_runs_dir()
        run_dir = self.make_run(runs_dir, "run-fixture-001")
        self.write_json(
            run_dir / "run_status.json",
            {
                "run_id": "run-fixture-001",
                "status": "completed_fixture",
                "ok": True,
                "terminal": True,
                "provenance": {"type": "fixture", "adapter": "fixture", "fixture_only": True},
            },
        )
        self.write_json(
            run_dir / "run_steps.json",
            {"run_id": "run-fixture-001", "status": "completed", "stages": {}},
        )

        payload = list_run_monitors(runs_dir)
        rendered = render_run_list(payload)

        self.assertEqual(payload["run_count"], 1)
        self.assertEqual(payload["runs"][0]["run_path"], "run-fixture-001")
        self.assertEqual(payload["runs"][0]["mode"]["kind"], "fixture")
        self.assertEqual(payload["runs_dir"], runs_dir.name)
        self.assertIn("fixture", rendered)
        self.assertNotIn(str(runs_dir), rendered)

    def test_live_assignment_overlay_updates_stale_task_queue(self) -> None:
        runs_dir = self.temp_runs_dir()
        run_dir = self.make_run(runs_dir, "run-live-001")
        self.write_json(
            run_dir / "research_tasks.json",
            {
                "run_id": "run-live-001",
                "task_count": 5,
                "tasks": [
                    {"id": "task_active", "state": "queued", "attempt": 0},
                    {"id": "task_failed", "state": "queued", "attempt": 0},
                    {"id": "task_completed", "state": "queued", "attempt": 0},
                    {"id": "task_retry_active", "state": "queued", "attempt": 0},
                    {"id": "task_queued", "state": "queued", "attempt": 0},
                ],
            },
        )
        self.write_jsonl(
            run_dir / "subagent_assignments.jsonl",
            [
                {"task_id": "task_active", "state": "assigned", "attempt": 1},
                {"task_id": "task_failed", "state": "assigned", "attempt": 2},
                {"task_id": "task_completed", "state": "assigned", "attempt": 1},
                {"task_id": "task_retry_active", "state": "assigned", "attempt": 2},
            ],
        )
        self.write_jsonl(
            run_dir / "run_trace.jsonl",
            [
                {
                    "stage": "parallel_orchestration",
                    "status": "running",
                    "task_id": "task_active",
                    "child_status": "running",
                    "event_type": "spawn_agent",
                },
                {
                    "stage": "parallel_orchestration",
                    "status": "failed",
                    "task_id": "task_failed",
                    "child_status": "failed",
                    "event_type": "wait",
                },
                {
                    "stage": "parallel_orchestration",
                    "status": "completed",
                    "task_id": "task_completed",
                    "child_status": "completed",
                    "event_type": "close_agent",
                },
                {
                    "stage": "parallel_orchestration",
                    "status": "failed",
                    "task_id": "task_retry_active",
                    "child_status": "failed",
                    "child_thread_id": "codex-exec-task_retry_active-attempt-1",
                    "event_type": "wait",
                },
            ],
        )

        detail = inspect_run_monitor(run_dir)

        self.assertEqual(detail["shards"]["queued"], 1)
        self.assertEqual(detail["shards"]["active"], 2)
        self.assertEqual(detail["shards"]["completed"], 1)
        self.assertEqual(detail["shards"]["failed"], 1)
        self.assertEqual(detail["shards"]["retried"], 2)
        self.assertEqual(detail["shards"]["planned"], 5)
        self.assertIn("completed=1", render_run_detail(detail))

    def test_real_parallel_failure_blocked_and_degraded_labels_are_honest(self) -> None:
        cases = [
            (
                "failed-real",
                {
                    "status": "failed_parallel_no_accepted_shards",
                    "ok": False,
                    "adapter": "codex-exec",
                    "parallel_degraded": False,
                    "needs_serial_handoff": True,
                    "evidence_source": {
                        "type": "failed_real_child_execution",
                        "adapter": "codex-exec",
                        "attempted_real_child_execution": True,
                    },
                },
                "failed_real_parallel",
                "failed real parallel",
                None,
            ),
            (
                "blocked-real",
                {
                    "status": "blocked_parallel_execution",
                    "ok": False,
                    "adapter": "codex-exec",
                    "parallel_degraded": False,
                    "needs_serial_handoff": True,
                    "evidence_source": {
                        "type": "blocked_parallel_execution",
                        "adapter": "codex-exec",
                        "attempted_real_child_execution": False,
                    },
                },
                "blocked_real_parallel",
                "blocked real parallel",
                None,
            ),
            (
                "plain-serial-fallback",
                {
                    "status": "completed_serial_handoff",
                    "ok": True,
                    "adapter": "serial-degraded",
                    "parallel_degraded": False,
                    "needs_serial_handoff": True,
                    "evidence_source": {
                        "type": "serial_handoff",
                        "adapter": "serial-degraded",
                    },
                },
                "serial_fallback",
                "serial fallback",
                None,
            ),
            (
                "degraded-real",
                {
                    "status": "degraded_serial_handoff_required",
                    "ok": False,
                    "adapter": "serial-degraded",
                    "parallel_degraded": True,
                    "degraded_reason": "codex_exec_missing_feature",
                    "needs_serial_handoff": True,
                    "evidence_source": {
                        "type": "serial_handoff",
                        "adapter": "serial-degraded",
                        "attempted_real_child_execution": True,
                    },
                },
                "degraded_parallel",
                "degraded parallel",
                "codex_exec_missing_feature",
            ),
        ]
        for suffix, parallel_status, expected_kind, expected_label, expected_reason in cases:
            with self.subTest(suffix=suffix):
                runs_dir = self.temp_runs_dir()
                run_dir = self.make_run(runs_dir, f"run-{suffix}")
                self.write_json(run_dir / "parallel_orchestration_status.json", parallel_status)

                detail = inspect_run_monitor(run_dir)
                rendered = render_run_detail(detail)

                self.assertEqual(detail["mode"]["kind"], expected_kind)
                self.assertEqual(detail["mode"]["label"], expected_label)
                self.assertEqual(detail["mode"]["degraded_reason"], expected_reason)
                self.assertIn(expected_label, rendered)
                if expected_kind == "degraded_parallel":
                    self.assertNotEqual(detail["mode"]["kind"], "serial_fallback")

        runs_dir = self.temp_runs_dir()
        run_dir = self.make_run(runs_dir, "run-degraded-run-status-only")
        self.write_json(
            run_dir / "run_status.json",
            {
                "run_id": "run-degraded-run-status-only",
                "selected_mode": "full-runner",
                "status": "degraded_serial_handoff_required",
                "ok": False,
                "terminal": True,
                "provenance": {
                    "type": "serial_handoff",
                    "adapter": "serial-degraded",
                    "attempted_real_child_execution": True,
                },
                "parallel": {
                    "parallel_degraded": True,
                    "needs_serial_handoff": True,
                    "degraded_reason": "codex_exec_missing_feature",
                },
                "fallback": {
                    "parallel_degraded": True,
                    "needs_serial_handoff": True,
                    "degraded_reason": "codex_exec_missing_feature",
                },
            },
        )

        detail = inspect_run_monitor(run_dir)

        self.assertEqual(detail["mode"]["kind"], "degraded_parallel")
        self.assertEqual(detail["mode"]["label"], "degraded parallel")
        self.assertEqual(detail["mode"]["degraded_reason"], "codex_exec_missing_feature")
        self.assertNotEqual(detail["mode"]["kind"], "serial_fallback")

    def test_nonterminal_run_status_does_not_hide_newer_parallel_status(self) -> None:
        runs_dir = self.temp_runs_dir()
        run_dir = self.make_run(runs_dir, "run-stale-status-001")
        self.write_json(
            run_dir / "run_status.json",
            {
                "run_id": "run-stale-status-001",
                "selected_mode": "full-runner",
                "status": "prepared",
                "ok": True,
                "terminal": False,
                "provenance": {
                    "type": "fixture",
                    "adapter": "fixture",
                    "fixture_only": True,
                },
            },
        )
        self.write_json(
            run_dir / "run_steps.json",
            {
                "run_id": "run-stale-status-001",
                "status": "needs_retry",
                "next_safe_stage": "parallel_orchestration",
                "stages": {
                    "parallel_orchestration": {
                        "stage": "parallel_orchestration",
                        "status": "failed",
                    },
                },
            },
        )
        self.write_json(
            run_dir / "parallel_orchestration_status.json",
            {
                "run_id": "run-stale-status-001",
                "status": "failed_parallel_no_accepted_shards",
                "ok": False,
                "adapter": "codex-exec",
                "parallel_degraded": False,
                "needs_serial_handoff": True,
                "evidence_source": {
                    "type": "failed_real_child_execution",
                    "adapter": "codex-exec",
                },
            },
        )

        detail = inspect_run_monitor(run_dir)

        self.assertEqual(detail["status"], "failed_parallel_no_accepted_shards")
        self.assertFalse(detail["ok"])
        self.assertFalse(detail["terminal"])
        self.assertEqual(detail["mode"]["kind"], "failed_real_parallel")

    def test_real_parallel_and_exhaustive_budget_state_are_visible(self) -> None:
        runs_dir = self.temp_runs_dir()
        run_dir = self.make_run(runs_dir, "run-real-001")
        self.write_json(
            run_dir / "research_tasks.json",
            {
                "run_id": "run-real-001",
                "task_count": 2,
                "tasks": [
                    {"id": "task_001", "state": "merged", "attempt": 1},
                    {"id": "task_002", "state": "merged", "attempt": 1},
                ],
            },
        )
        self.write_json(
            run_dir / "merge_status.json",
            {
                "run_id": "run-real-001",
                "status": "completed",
                "accepted_shards": [
                    {"task_id": "task_001", "path": str(run_dir / "shards/task_001.json")},
                    {"task_id": "task_002", "path": str(run_dir / "shards/task_002.json")},
                ],
                "failure_counts": {"failed_tasks": 0, "blocked_tasks": 0},
                "evidence_source": {
                    "type": "real_child_execution",
                    "adapter": "codex-exec",
                    "real_child_execution": True,
                    "real_use_e2e_eligible": True,
                },
            },
        )
        self.write_json(
            run_dir / "parallel_orchestration_status.json",
            {
                "run_id": "run-real-001",
                "status": "completed_parallel",
                "ok": True,
                "adapter": "codex-exec",
                "parallel_degraded": False,
                "planned_task_count": 2,
                "needs_serial_handoff": False,
                "evidence_source": {
                    "type": "real_child_execution",
                    "adapter": "codex-exec",
                    "real_child_execution": True,
                    "real_use_e2e_eligible": True,
                },
            },
        )
        self.write_json(
            run_dir / "budget_estimate.json",
            {
                "budget_preset": "exhaustive",
                "confirmation": {"required": True, "provided": False},
                "effective_caps": {
                    "max_concurrent_codex_subagents": 100,
                    "max_cost_usd": 100.0,
                },
                "high_water_cost_bounds": {
                    "upper_bound_usd": 88.0,
                    "max_cost_usd": 100.0,
                    "within_max_cost": True,
                },
            },
        )

        detail = inspect_run_monitor(run_dir)
        listing = list_run_monitors(runs_dir)
        rendered_list = render_run_list(listing)

        self.assertEqual(detail["mode"]["kind"], "real_parallel")
        self.assertEqual(detail["shards"]["accepted"], 2)
        self.assertEqual(detail["shards"]["merged"], 2)
        self.assertEqual(detail["budget"]["preset"], "exhaustive")
        self.assertTrue(detail["budget"]["confirmation_required"])
        self.assertFalse(detail["budget"]["confirmation_provided"])
        self.assertEqual(detail["budget"]["max_cost_usd"], 100.0)
        self.assertIn("real parallel", rendered_list)
        self.assertIn("exhaustive sub=100 confirm=no/yes cap=100.00 USD", rendered_list)

    def test_standalone_run_prefers_terminal_stage_over_stale_planning_status(self) -> None:
        runs_dir = self.temp_runs_dir()
        run_dir = self.make_run(runs_dir, "run-standalone-001")
        self.write_json(
            run_dir / "run_status.json",
            {
                "run_id": "run-standalone-001",
                "status": "prepared",
                "ok": True,
                "terminal": False,
            },
        )
        self.write_json(
            run_dir / "status.json",
            {
                "run_id": "run-standalone-001",
                "status": "awaiting_search_results",
                "artifacts": {"status": str(run_dir / "status.json")},
            },
        )
        self.write_json(
            run_dir / "run_steps.json",
            {
                "run_id": "run-standalone-001",
                "status": "completed",
                "next_safe_stage": None,
                "stages": {
                    "planning": {"stage": "planning", "status": "completed"},
                    "synthesize": {"stage": "synthesize", "status": "completed"},
                },
            },
        )
        self.write_json(
            run_dir / "report_status.json",
            {
                "run_id": "run-standalone-001",
                "status": "completed",
                "artifacts": {
                    "report": str(run_dir / "report.md"),
                    "report_status": str(run_dir / "report_status.json"),
                },
            },
        )

        detail = inspect_run_monitor(run_dir)

        self.assertEqual(detail["phase"]["stage"], "completed")
        self.assertEqual(detail["status"], "completed")
        self.assertTrue(detail["terminal"])
        self.assertNotEqual(detail["status"], "awaiting_search_results")
        self.assertIn("report_status", detail["artifacts"])

    def test_monitor_detail_cli_outputs_json(self) -> None:
        runs_dir = self.temp_runs_dir()
        run_dir = self.make_run(runs_dir, "run-manual-001")
        self.write_json(
            run_dir / "run_status.json",
            {
                "run_id": "run-manual-001",
                "selected_mode": "manual-handoff",
                "status": "manual_sources_ingested",
                "ok": True,
                "terminal": True,
                "provenance": {"type": "manual_handoff", "manual_handoff": True},
            },
        )

        result = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "monitor-detail",
                "--run",
                "run-manual-001",
                "--runs-dir",
                str(runs_dir),
                "--json",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"]["kind"], "manual")
        self.assertEqual(payload["run_id"], "run-manual-001")


if __name__ == "__main__":
    unittest.main()
