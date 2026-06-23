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

from deepresearch import (  # noqa: E402
    RunStepStateError,
    ingest_manual_sources,
    ingest_run,
    inspect_run_state,
    prepare_run,
    read_trace_records,
    run_steps_path,
    transition_stage,
)


class RunStateTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def base_search_result(self, **overrides) -> dict:
        result = {
            "id": "sr_001",
            "task_id": "task_search_001",
            "angle_id": "angle_001",
            "route": "text_only",
            "provider": "codex-native",
            "query": "example search question",
            "url": "https://example.com/source",
            "title": "Example Source",
            "snippet": "Example snippet",
            "result_type": "web",
            "rank": 1,
            "freshness_requirement": "any",
            "published_at": None,
            "accessed_at": "2026-06-22T00:00:00Z",
            "language": "en",
            "region": "US",
            "policy_decision": "allowed",
            "policy_flags": [],
            "raw_provider_metadata": {},
        }
        result.update(overrides)
        return result

    def write_search_results(self, run_dir: Path, records: list[dict]) -> None:
        payload = "\n".join(json.dumps(record) for record in records) + "\n"
        (run_dir / "search_results.jsonl").write_text(payload, encoding="utf-8")

    def test_prepare_initializes_run_steps_with_completed_planning(self) -> None:
        prepared = prepare_run(
            question="state machine preparation",
            runs_dir=self.temp_runs_dir(),
            route="text_only",
        )
        run_dir = Path(prepared["run_dir"])

        state = inspect_run_state(run_dir)

        self.assertTrue(run_steps_path(run_dir).is_file())
        self.assertEqual(prepared["artifacts"]["run_steps"], str(run_dir / "run_steps.json"))
        self.assertEqual(state["next_safe_stage"], "ingest")
        self.assertFalse(state["next_stage_retryable"])
        stages = {stage["stage"]: stage for stage in state["stages"]}
        self.assertEqual(stages["planning"]["status"], "completed")
        self.assertEqual(stages["ingest"]["status"], "pending")
        self.assertEqual(stages["fetch_claims"]["status"], "pending")
        self.assertEqual(
            self.read_json(run_dir / "run_steps.json")["transition_rules"]["completed"],
            ["completed", "failed", "skipped"],
        )
        trace = read_trace_records(run_dir / "run_trace.jsonl")
        self.assertIn("run_steps", trace[0]["artifacts"])

    def test_completed_stage_rerun_is_explicitly_skipped(self) -> None:
        first = ingest_manual_sources(
            question="state machine manual rerun",
            runs_dir=self.temp_runs_dir(),
            urls=["https://example.com/manual-source"],
        )
        run_dir = Path(first["run_dir"])

        second = ingest_manual_sources(
            run=run_dir,
            runs_dir=run_dir.parent,
            urls=["https://example.com/second-source"],
        )
        state = inspect_run_state(run_dir)
        stages = {stage["stage"]: stage for stage in state["stages"]}
        trace = read_trace_records(run_dir / "run_trace.jsonl")

        self.assertEqual(first["status"], "manual_sources_ingested")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(second["skip_reason"], "stage_already_completed")
        self.assertEqual(stages["ingest_manual"]["status"], "completed")
        self.assertEqual(stages["ingest_manual"]["skip_reason"], "stage_already_completed")
        self.assertEqual(stages["ingest_manual"]["last_rerun_status"], "skipped")
        self.assertEqual(stages["ingest_manual"]["history"][-1]["to"], "completed")
        self.assertEqual(stages["ingest_manual"]["history"][-1]["rerun_status"], "skipped")
        self.assertEqual(state["next_safe_stage"], "enforce_guardrails")
        self.assertEqual(trace[-1]["stage"], "ingest_manual")
        self.assertEqual(trace[-1]["status"], "skipped")
        self.assertIn("run_steps", second["artifacts"])

    def test_completed_ingest_rerun_revalidates_and_stays_completed(self) -> None:
        prepared = prepare_run(
            question="state machine idempotent ingest",
            runs_dir=self.temp_runs_dir(),
            route="text_only",
        )
        run_dir = Path(prepared["run_dir"])
        self.write_search_results(run_dir, [self.base_search_result()])

        first = ingest_run(run=run_dir)
        first_state = inspect_run_state(run_dir)
        first_ingest = {
            stage["stage"]: stage for stage in first_state["stages"]
        }["ingest"]
        second = ingest_run(run=run_dir)
        second_state = inspect_run_state(run_dir)
        second_ingest = {
            stage["stage"]: stage for stage in second_state["stages"]
        }["ingest"]

        self.assertEqual(first["status"], "ingested")
        self.assertEqual(second["status"], "ingested")
        self.assertEqual(second_ingest["status"], "completed")
        self.assertEqual(second_state["next_safe_stage"], "fetch_claims")
        self.assertGreater(
            len(second_ingest["trace_event_ids"]),
            len(first_ingest["trace_event_ids"]),
        )
        self.assertEqual(second_ingest["history"][-1]["from"], "completed")
        self.assertEqual(second_ingest["history"][-1]["to"], "completed")

    def test_invalid_transition_reports_machine_readable_error(self) -> None:
        prepared = prepare_run(
            question="invalid transition",
            runs_dir=self.temp_runs_dir(),
        )
        run_dir = Path(prepared["run_dir"])

        with self.assertRaises(RunStepStateError) as raised:
            transition_stage(
                run_dir,
                "planning",
                "running",
                reason="test_invalid_transition",
            )

        payload = json.loads(str(raised.exception))
        self.assertEqual(payload["code"], "invalid_state_transition")
        self.assertEqual(payload["stage"], "planning")
        self.assertEqual(payload["from_status"], "completed")
        self.assertEqual(payload["to_status"], "running")

    def test_failed_stage_is_retryable_and_can_complete_after_retry(self) -> None:
        prepared = prepare_run(
            question="retry failed ingest",
            runs_dir=self.temp_runs_dir(),
            route="text_only",
        )
        run_dir = Path(prepared["run_dir"])

        failed = ingest_run(run=run_dir)
        failed_state = inspect_run_state(run_dir)
        failed_stages = {stage["stage"]: stage for stage in failed_state["stages"]}

        self.assertEqual(failed["status"], "blocked_missing_search_handoff")
        self.assertEqual(failed_state["next_safe_stage"], "ingest")
        self.assertTrue(failed_state["next_stage_retryable"])
        self.assertEqual(failed_stages["ingest"]["status"], "failed")
        self.assertTrue(failed_stages["ingest"]["retryable"])

        self.write_search_results(run_dir, [self.base_search_result()])
        retried = ingest_run(run=run_dir)
        retried_state = inspect_run_state(run_dir)
        retried_stages = {stage["stage"]: stage for stage in retried_state["stages"]}

        self.assertEqual(retried["status"], "ingested")
        self.assertEqual(retried_stages["ingest"]["status"], "completed")
        self.assertFalse(retried_stages["ingest"]["retryable"])
        self.assertEqual(retried_state["next_safe_stage"], "fetch_claims")

    def test_interrupted_running_stage_is_inspectable_by_run_id(self) -> None:
        runs_dir = self.temp_runs_dir()
        prepared = prepare_run(
            question="interrupted stage",
            runs_dir=runs_dir,
            route="text_only",
        )
        run_dir = Path(prepared["run_dir"])
        transition_stage(
            run_dir,
            "ingest",
            "running",
            reason="test_interrupted_stage",
        )

        command = subprocess.run(
            [
                str(RUNNER),
                "run-status",
                "--run",
                prepared["run_id"],
                "--runs-dir",
                str(runs_dir),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(command.returncode, 0, command.stderr)
        payload = json.loads(command.stdout)
        self.assertEqual(payload["next_safe_stage"], "ingest")
        self.assertTrue(payload["next_stage_retryable"])
        stages = {stage["stage"]: stage for stage in payload["stages"]}
        self.assertEqual(stages["ingest"]["status"], "running")
        self.assertTrue(stages["ingest"]["retryable"])


if __name__ == "__main__":
    unittest.main()
