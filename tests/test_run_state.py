from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "plugins" / "codex-deepresearch" / "scripts" / "codex-deepresearch"
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch import (  # noqa: E402
    RunStepStateError,
    begin_stage,
    enforce_guardrails,
    fetch_claims,
    ingest_manual_sources,
    ingest_run,
    ingest_vision_observations,
    inspect_run_state,
    prepare_run,
    read_trace_records,
    run_steps_path,
    synthesize_report,
    transition_stage,
    verify_claims,
)
from deepresearch.trace import record_stage_trace  # noqa: E402


fetch_claims_module = importlib.import_module("deepresearch.fetch_claims")


class FakeResponse:
    def __init__(
        self,
        content: bytes,
        *,
        mime_type: str = "text/html",
        status: int = 200,
        url: str = "https://example.com/source",
    ) -> None:
        self._content = content
        self.status = status
        self.headers = Message()
        self.headers.set_type(mime_type)
        self._url = url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self._content

    def geturl(self) -> str:
        return self._url


class RunStateTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    def set_status_timestamps(
        self,
        path: Path,
        timestamp: str,
        *,
        keys: tuple[str, ...] = ("created_at",),
    ) -> None:
        payload = self.read_json(path)
        for key in keys:
            payload[key] = timestamp
        self.write_json(path, payload)

    def rewrite_trace(
        self,
        run_dir: Path,
        *,
        stage_timestamps: dict[str, str],
        drop_stages: set[str] | None = None,
    ) -> None:
        drop_stages = drop_stages or set()
        records = []
        for record in read_trace_records(run_dir / "run_trace.jsonl"):
            stage = record.get("stage")
            if stage in drop_stages:
                continue
            timestamp = stage_timestamps.get(stage)
            if timestamp is not None:
                record["timestamp"] = timestamp
            records.append(record)
        payload = "".join(
            json.dumps(record, sort_keys=True) + "\n"
            for record in records
        )
        (run_dir / "run_trace.jsonl").write_text(payload, encoding="utf-8")

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

    def run_status_payload(self, runs_dir: Path, run_id: str) -> dict:
        command = subprocess.run(
            [
                str(RUNNER),
                "run-status",
                "--run",
                run_id,
                "--runs-dir",
                str(runs_dir),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(command.returncode, 0, command.stderr)
        return json.loads(command.stdout)

    def create_stale_downstream_artifact_run(self, runs_dir: Path) -> tuple[dict, Path]:
        prepared = prepare_run(
            question="state machine stale downstream artifact reconstruction",
            runs_dir=runs_dir,
            route="text_only",
        )
        run_dir = Path(prepared["run_dir"])
        self.write_search_results(run_dir, [self.base_search_result()])
        ingest = ingest_run(run=run_dir)
        self.assertEqual(ingest["status"], "ingested")

        early_verify = verify_claims(run=run_dir)
        early_report = synthesize_report(run=run_dir)
        self.assertEqual(early_verify["status"], "completed")
        self.assertEqual(early_report["status"], "completed")
        self.assertTrue((run_dir / "verification_matrix_status.json").is_file())
        self.assertTrue((run_dir / "report_status.json").is_file())

        html = b"""
        <html>
          <body>
            <p>The stale artifact reconstruction test extracts a source linked claim.</p>
          </body>
        </html>
        """
        with mock.patch.object(
            fetch_claims_module,
            "urlopen",
            return_value=FakeResponse(html),
        ):
            fetch = fetch_claims(run=run_dir)
        self.assertEqual(fetch["status"], "completed")
        vision = ingest_vision_observations(run=run_dir, provider="codex-interactive")
        self.assertEqual(vision["status"], "no_visual_tasks")
        guardrails = enforce_guardrails(run=run_dir)
        self.assertEqual(guardrails["status"], "completed")
        return prepared, run_dir

    def stamp_stale_downstream_artifacts(self, run_dir: Path) -> None:
        self.set_status_timestamps(run_dir / "status.json", "2026-06-22T00:00:00Z")
        self.set_status_timestamps(run_dir / "ingest_status.json", "2026-06-22T00:00:05Z")
        self.set_status_timestamps(
            run_dir / "verification_matrix_status.json",
            "2026-06-22T00:00:10Z",
        )
        self.set_status_timestamps(
            run_dir / "report_status.json",
            "2026-06-22T00:00:11Z",
            keys=("created_at", "generated_at"),
        )
        self.set_status_timestamps(
            run_dir / "fetch_claims_status.json",
            "2026-06-22T00:00:20Z",
        )
        self.set_status_timestamps(
            run_dir / "vision_ingest_status.json",
            "2026-06-22T00:00:30Z",
        )
        self.set_status_timestamps(
            run_dir / "guardrails_status.json",
            "2026-06-22T00:00:40Z",
        )

    def stamp_stale_downstream_trace_without_downstream(self, run_dir: Path) -> None:
        self.rewrite_trace(
            run_dir,
            stage_timestamps={
                "planning": "2026-06-22T00:00:00Z",
                "ingest": "2026-06-22T00:00:05Z",
                "fetch_claims": "2026-06-22T00:00:20Z",
                "ingest_vision": "2026-06-22T00:00:30Z",
                "enforce_guardrails": "2026-06-22T00:00:40Z",
                "verify_claims": "2026-06-22T00:00:10Z",
                "synthesize": "2026-06-22T00:00:11Z",
            },
            drop_stages={"verify_claims", "synthesize"},
        )

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
            ["running", "completed", "failed", "skipped"],
        )
        trace = read_trace_records(run_dir / "run_trace.jsonl")
        self.assertIn("run_steps", trace[0]["artifacts"])

    def test_run_status_reconstructs_deleted_run_steps_from_trace(self) -> None:
        runs_dir = self.temp_runs_dir()
        prepared = prepare_run(
            question="state machine reconstruction",
            runs_dir=runs_dir,
            route="text_only",
        )
        run_dir = Path(prepared["run_dir"])
        run_steps_path(run_dir).unlink()

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
        stages = {stage["stage"]: stage for stage in payload["stages"]}
        self.assertTrue(run_steps_path(run_dir).is_file())
        self.assertEqual(payload["next_safe_stage"], "ingest")
        self.assertFalse(payload["next_stage_retryable"])
        self.assertEqual(stages["planning"]["status"], "completed")
        self.assertEqual(stages["ingest"]["status"], "pending")

    def test_run_status_reconstructs_deleted_run_steps_from_status_without_trace(self) -> None:
        runs_dir = self.temp_runs_dir()
        prepared = prepare_run(
            question="state machine status artifact reconstruction",
            runs_dir=runs_dir,
            route="text_only",
        )
        run_dir = Path(prepared["run_dir"])
        run_steps_path(run_dir).unlink()
        (run_dir / "run_trace.jsonl").unlink()

        state = inspect_run_state(run_dir)
        stages = {stage["stage"]: stage for stage in state["stages"]}

        self.assertTrue(run_steps_path(run_dir).is_file())
        self.assertEqual(state["next_safe_stage"], "ingest")
        self.assertFalse(state["next_stage_retryable"])
        self.assertEqual(stages["planning"]["status"], "completed")
        self.assertEqual(stages["ingest"]["status"], "pending")

    def test_reconstruction_merges_status_artifacts_after_partial_trace(self) -> None:
        runs_dir = self.temp_runs_dir()
        prepared = prepare_run(
            question="state machine partial trace reconstruction",
            runs_dir=runs_dir,
            route="text_only",
        )
        run_dir = Path(prepared["run_dir"])
        self.write_search_results(run_dir, [self.base_search_result()])
        ingest = ingest_run(run=run_dir)
        self.assertEqual(ingest["status"], "ingested")

        records = read_trace_records(run_dir / "run_trace.jsonl")
        self.assertEqual([record["stage"] for record in records], ["planning", "ingest"])
        self.set_status_timestamps(run_dir / "status.json", "2026-06-22T00:00:01Z")
        self.set_status_timestamps(run_dir / "ingest_status.json", "2026-06-22T00:00:02Z")
        records[0]["timestamp"] = "2026-06-22T00:00:01Z"
        (run_dir / "run_trace.jsonl").write_text(
            json.dumps(records[0]) + "\n",
            encoding="utf-8",
        )
        run_steps_path(run_dir).unlink()

        state = inspect_run_state(run_dir)
        stages = {stage["stage"]: stage for stage in state["stages"]}

        self.assertTrue(run_steps_path(run_dir).is_file())
        self.assertEqual(state["next_safe_stage"], "fetch_claims")
        self.assertFalse(state["next_stage_retryable"])
        self.assertEqual(stages["planning"]["status"], "completed")
        self.assertEqual(stages["ingest"]["status"], "completed")
        self.assertEqual(
            stages["ingest"]["artifacts"]["ingest_status"],
            str(run_dir / "ingest_status.json"),
        )

    def test_manual_source_reconstruction_restores_ordered_skips(self) -> None:
        runs_dir = self.temp_runs_dir()
        result = ingest_manual_sources(
            question="state machine manual reconstruction",
            runs_dir=runs_dir,
            urls=["https://example.com/manual-source"],
        )
        run_dir = Path(result["run_dir"])
        before = inspect_run_state(run_dir)
        self.assertEqual(before["next_safe_stage"], "enforce_guardrails")
        run_steps_path(run_dir).unlink()

        state = inspect_run_state(run_dir)
        stages = {stage["stage"]: stage for stage in state["stages"]}

        self.assertTrue(run_steps_path(run_dir).is_file())
        self.assertEqual(state["next_safe_stage"], "enforce_guardrails")
        self.assertFalse(state["next_stage_retryable"])
        for stage in ("planning", "ingest", "fetch_claims", "ingest_vision"):
            self.assertEqual(stages[stage]["status"], "skipped")
            self.assertEqual(stages[stage]["skip_reason"], "manual_sources_run")
            self.assertEqual(
                stages[stage]["reconstructed_skip"]["source"],
                "manual_source_ingest",
            )
        self.assertEqual(stages["ingest_manual"]["status"], "completed")

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
        self.assertEqual(second_ingest["history"][-2]["from"], "completed")
        self.assertEqual(second_ingest["history"][-2]["to"], "running")
        self.assertEqual(second_ingest["history"][-2]["reason"], "rerun_completed_stage")
        self.assertEqual(second_ingest["history"][-1]["from"], "running")
        self.assertEqual(second_ingest["history"][-1]["to"], "completed")
        self.assertEqual(second_ingest["last_rerun_status"], "completed")
        self.assertEqual(second_ingest["previous_terminal_status"]["status"], "completed")
        self.assertEqual(second_ingest["previous_terminal_status"]["attempt"], first_ingest["attempt"])

    def test_completed_upstream_rerun_resets_downstream_terminal_stages(self) -> None:
        prepared = prepare_run(
            question="state machine stale downstream reset",
            runs_dir=self.temp_runs_dir(),
            route="text_only",
        )
        run_dir = Path(prepared["run_dir"])
        for stage in (
            "ingest",
            "fetch_claims",
            "ingest_vision",
            "enforce_guardrails",
            "verify_claims",
            "synthesize",
        ):
            transition_stage(run_dir, stage, "running", reason="test_stage_started")
            transition_stage(run_dir, stage, "completed", reason="test_stage_completed")
        completed_state = inspect_run_state(run_dir)

        started = begin_stage(run_dir, "fetch_claims")
        transition_stage(run_dir, "fetch_claims", "completed", reason="test_rerun_completed")
        rerun_state = inspect_run_state(run_dir)
        stages = {stage["stage"]: stage for stage in rerun_state["stages"]}

        self.assertEqual(completed_state["status"], "completed")
        self.assertIsNone(completed_state["next_safe_stage"])
        self.assertEqual(started.status, "running")
        self.assertFalse(started.skipped)
        self.assertEqual(rerun_state["next_safe_stage"], "ingest_vision")
        self.assertEqual(stages["fetch_claims"]["status"], "completed")
        self.assertEqual(stages["ingest_vision"]["status"], "pending")
        self.assertEqual(stages["enforce_guardrails"]["status"], "pending")
        self.assertEqual(stages["verify_claims"]["status"], "pending")
        self.assertEqual(stages["synthesize"]["status"], "pending")
        self.assertEqual(
            stages["enforce_guardrails"]["stale_reset"]["status"],
            "stale-reset",
        )
        self.assertEqual(
            stages["enforce_guardrails"]["stale_reset"]["upstream_stage"],
            "fetch_claims",
        )
        self.assertEqual(
            stages["enforce_guardrails"]["stale_terminal_status"]["status"],
            "completed",
        )
        self.assertEqual(
            stages["enforce_guardrails"]["history"][-1]["reason"],
            "stale_reset_after_upstream_rerun",
        )

    def test_first_time_upstream_completion_resets_early_downstream_terminal_stages(self) -> None:
        runs_dir = self.temp_runs_dir()
        prepared = prepare_run(
            question="state machine first completion stale downstream reset",
            runs_dir=runs_dir,
            route="text_only",
        )
        run_dir = Path(prepared["run_dir"])
        page = runs_dir / "source.html"
        page.write_text(
            (
                "<html><body><p>The first completion reset test extracts a "
                "source linked claim.</p></body></html>"
            ),
            encoding="utf-8",
        )
        self.write_search_results(run_dir, [self.base_search_result(title="Local Source")])
        ingest = ingest_run(run=run_dir)
        self.assertEqual(ingest["status"], "ingested")
        evidence_path = run_dir / "evidence.json"
        evidence = self.read_json(evidence_path)
        evidence["sources"][0]["url"] = page.resolve().as_uri()
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        fetch_queue_path = run_dir / "fetch_queue.json"
        fetch_queue = self.read_json(fetch_queue_path)
        fetch_queue["entries"][0]["url"] = page.resolve().as_uri()
        fetch_queue_path.write_text(json.dumps(fetch_queue), encoding="utf-8")

        for stage in ("verify_claims", "synthesize"):
            transition_stage(run_dir, stage, "running", reason="test_early_stage_started")
            transition_stage(run_dir, stage, "completed", reason="test_early_stage_completed")

        fetch = fetch_claims(run=run_dir)
        self.assertEqual(fetch["status"], "completed")
        transition_stage(run_dir, "ingest_vision", "running", reason="test_stage_started")
        transition_stage(run_dir, "ingest_vision", "skipped", reason="test_no_visual_tasks")
        transition_stage(run_dir, "enforce_guardrails", "running", reason="test_stage_started")
        transition_stage(run_dir, "enforce_guardrails", "completed", reason="test_stage_completed")

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
        stages = {stage["stage"]: stage for stage in payload["stages"]}
        self.assertEqual(payload["next_safe_stage"], "verify_claims")
        self.assertFalse(payload["next_stage_retryable"])
        self.assertEqual(stages["ingest"]["status"], "completed")
        self.assertEqual(stages["fetch_claims"]["status"], "completed")
        self.assertEqual(stages["verify_claims"]["status"], "pending")
        self.assertEqual(stages["synthesize"]["status"], "pending")
        self.assertEqual(
            stages["verify_claims"]["stale_reset"]["reason"],
            "stale_reset_after_upstream_completion",
        )
        self.assertEqual(
            stages["verify_claims"]["stale_reset"]["upstream_stage"],
            "fetch_claims",
        )
        self.assertEqual(
            stages["verify_claims"]["stale_terminal_status"]["status"],
            "completed",
        )

    def test_reconstruction_rejects_stale_downstream_status_artifacts(self) -> None:
        runs_dir = self.temp_runs_dir()
        prepared, run_dir = self.create_stale_downstream_artifact_run(runs_dir)

        live = self.run_status_payload(runs_dir, prepared["run_id"])
        live_stages = {stage["stage"]: stage for stage in live["stages"]}
        self.assertEqual(live["next_safe_stage"], "verify_claims")
        self.assertEqual(live_stages["verify_claims"]["status"], "pending")
        self.assertEqual(live_stages["synthesize"]["status"], "pending")
        self.assertEqual(
            live_stages["verify_claims"]["stale_reset"]["reason"],
            "stale_reset_after_upstream_completion",
        )

        self.stamp_stale_downstream_artifacts(run_dir)
        self.stamp_stale_downstream_trace_without_downstream(run_dir)
        run_steps_path(run_dir).unlink()
        reconstructed = self.run_status_payload(runs_dir, prepared["run_id"])
        stages = {stage["stage"]: stage for stage in reconstructed["stages"]}

        self.assertTrue(run_steps_path(run_dir).is_file())
        self.assertEqual(reconstructed["next_safe_stage"], "verify_claims")
        self.assertFalse(reconstructed["next_stage_retryable"])
        self.assertEqual(stages["verify_claims"]["status"], "pending")
        self.assertEqual(stages["synthesize"]["status"], "pending")
        self.assertEqual(
            stages["verify_claims"]["stale_reset"]["upstream_stage"],
            "enforce_guardrails",
        )
        self.assertEqual(
            stages["verify_claims"]["stale_terminal_status"]["status"],
            "completed",
        )
        self.assertEqual(
            stages["synthesize"]["stale_reset"]["upstream_stage"],
            "enforce_guardrails",
        )
        self.assertEqual(
            stages["synthesize"]["stale_terminal_status"]["status"],
            "completed",
        )

    def test_reconstruction_rejects_stale_downstream_status_artifacts_without_trace(self) -> None:
        runs_dir = self.temp_runs_dir()
        prepared, run_dir = self.create_stale_downstream_artifact_run(runs_dir)
        self.stamp_stale_downstream_artifacts(run_dir)
        (run_dir / "run_trace.jsonl").unlink()
        run_steps_path(run_dir).unlink()

        reconstructed = self.run_status_payload(runs_dir, prepared["run_id"])
        stages = {stage["stage"]: stage for stage in reconstructed["stages"]}

        self.assertTrue(run_steps_path(run_dir).is_file())
        self.assertEqual(reconstructed["next_safe_stage"], "verify_claims")
        self.assertFalse(reconstructed["next_stage_retryable"])
        self.assertEqual(stages["fetch_claims"]["status"], "completed")
        self.assertEqual(stages["enforce_guardrails"]["status"], "completed")
        self.assertEqual(stages["verify_claims"]["status"], "pending")
        self.assertEqual(stages["synthesize"]["status"], "pending")
        self.assertEqual(
            stages["verify_claims"]["stale_reset"]["upstream_stage"],
            "enforce_guardrails",
        )
        self.assertEqual(
            stages["verify_claims"]["stale_terminal_status"]["status"],
            "completed",
        )
        self.assertEqual(
            stages["synthesize"]["stale_reset"]["upstream_stage"],
            "enforce_guardrails",
        )
        self.assertEqual(
            stages["synthesize"]["stale_terminal_status"]["status"],
            "completed",
        )

    def test_completed_stage_rerun_is_retryable_before_trace_completion(self) -> None:
        prepared = prepare_run(
            question="state machine interrupted completed rerun",
            runs_dir=self.temp_runs_dir(),
            route="text_only",
        )
        run_dir = Path(prepared["run_dir"])
        self.write_search_results(run_dir, [self.base_search_result()])
        first = ingest_run(run=run_dir)
        completed_state = inspect_run_state(run_dir)
        completed_ingest = {
            stage["stage"]: stage for stage in completed_state["stages"]
        }["ingest"]

        started = begin_stage(run_dir, "ingest")
        interrupted_state = inspect_run_state(run_dir)
        interrupted_ingest = {
            stage["stage"]: stage for stage in interrupted_state["stages"]
        }["ingest"]

        self.assertEqual(first["status"], "ingested")
        self.assertEqual(started.status, "running")
        self.assertFalse(started.skipped)
        self.assertEqual(interrupted_state["next_safe_stage"], "ingest")
        self.assertTrue(interrupted_state["next_stage_retryable"])
        self.assertEqual(interrupted_ingest["status"], "running")
        self.assertTrue(interrupted_ingest["retryable"])
        self.assertEqual(interrupted_ingest["attempt"], completed_ingest["attempt"] + 1)
        self.assertEqual(interrupted_ingest["previous_terminal_status"]["status"], "completed")
        self.assertEqual(
            interrupted_ingest["previous_terminal_status"]["attempt"],
            completed_ingest["attempt"],
        )
        self.assertEqual(interrupted_ingest["last_rerun_status"], "running")
        self.assertEqual(interrupted_ingest["history"][-1]["from"], "completed")
        self.assertEqual(interrupted_ingest["history"][-1]["to"], "running")

    def test_completed_stage_rerun_skip_keeps_completed_primary_state(self) -> None:
        prepared = prepare_run(
            question="state machine completed rerun skip",
            runs_dir=self.temp_runs_dir(),
            route="text_only",
        )
        run_dir = Path(prepared["run_dir"])

        started = begin_stage(run_dir, "planning")
        record_stage_trace(
            run_dir,
            stage="planning",
            agent_role="test_agent",
            status_payload={
                "run_id": prepared["run_id"],
                "status": "skipped",
                "skip_reason": "test_rerun_no_work",
                "artifacts": {},
            },
            prompt_summary="Exercise skipped status after completed-stage rerun start.",
            tool_call_summary="Recorded skipped rerun status without mutating domain artifacts.",
        )
        state = inspect_run_state(run_dir)
        planning = {stage["stage"]: stage for stage in state["stages"]}["planning"]

        self.assertEqual(started.status, "running")
        self.assertEqual(planning["status"], "completed")
        self.assertFalse(planning["retryable"])
        self.assertEqual(planning["skip_reason"], "test_rerun_no_work")
        self.assertEqual(planning["last_rerun_status"], "skipped")
        self.assertEqual(planning["previous_terminal_status"]["status"], "completed")
        self.assertEqual(planning["history"][-1]["from"], "running")
        self.assertEqual(planning["history"][-1]["to"], "completed")
        self.assertEqual(planning["history"][-1]["rerun_status"], "skipped")

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
                "pending",
                reason="test_invalid_transition",
            )

        payload = json.loads(str(raised.exception))
        self.assertEqual(payload["code"], "invalid_state_transition")
        self.assertEqual(payload["stage"], "planning")
        self.assertEqual(payload["from_status"], "completed")
        self.assertEqual(payload["to_status"], "pending")

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
