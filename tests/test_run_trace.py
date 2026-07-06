from __future__ import annotations

import json
import importlib
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
    fetch_claims,
    ingest_run,
    ingest_vision_observations,
    prepare_run,
    read_trace_records,
    synthesize_report,
    validate_trace_file,
    validate_trace_record,
    verify_claims,
    enforce_guardrails,
)


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


class RunTraceTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

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

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_prepare_creates_run_start_planning_trace(self) -> None:
        result = prepare_run(
            question="example search question",
            runs_dir=self.temp_runs_dir(),
            angles=["primary source discovery"],
        )
        run_dir = Path(result["run_dir"])
        trace_path = run_dir / "run_trace.jsonl"

        self.assertTrue(trace_path.is_file())
        validation = validate_trace_file(trace_path)
        self.assertTrue(validation.valid, validation.to_dict())
        records = read_trace_records(trace_path)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["event_type"], "run_start")
        self.assertEqual(record["stage"], "planning")
        self.assertEqual(record["agent_role"], "planner")
        self.assertEqual(record["status"], "awaiting_search_results")
        self.assertIn("run_trace", record["artifacts"])

        status = self.read_json(run_dir / "status.json")
        self.assertEqual(status["artifacts"]["run_trace"], str(trace_path))
        self.assertEqual(result["artifacts"]["run_trace"], str(trace_path))

    def test_pipeline_stages_append_trace_records(self) -> None:
        prepared = prepare_run(
            question="example search question",
            runs_dir=self.temp_runs_dir(),
            route="text_only",
            angles=["primary source discovery"],
        )
        run_dir = Path(prepared["run_dir"])
        self.write_search_results(run_dir, [self.base_search_result()])

        ingest = ingest_run(run=run_dir)
        self.assertEqual(ingest["status"], "ingested")

        html = b"""
        <html>
          <body>
            <p>The run trace JSONL test preserves compact stage records for every runner stage.</p>
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
        verified = verify_claims(run=run_dir)
        self.assertEqual(verified["status"], "completed")
        report = synthesize_report(run=run_dir)
        self.assertEqual(report["status"], "completed")

        trace_path = run_dir / "run_trace.jsonl"
        records = read_trace_records(trace_path)
        self.assertEqual(
            [record["stage"] for record in records],
            [
                "planning",
                "ingest",
                "fetch_claims",
                "ingest_vision",
                "enforce_guardrails",
                "verify_claims",
                "synthesize",
            ],
        )
        self.assertEqual(len({record["event_id"] for record in records}), len(records))
        for record in records:
            self.assertTrue(validate_trace_record(record).valid, record)
            self.assertIn("run_trace", record["artifacts"])
            self.assertLessEqual(len(record["prompt_summary"]), 500)
            self.assertLessEqual(len(record["output_preview"]), 700)

        status_files = [
            "ingest_status.json",
            "fetch_claims_status.json",
            "vision_ingest_status.json",
            "guardrails_status.json",
            "verification_matrix_status.json",
            "report_status.json",
        ]
        for filename in status_files:
            status = self.read_json(run_dir / filename)
            expected_trace = "run_trace.jsonl" if filename == "report_status.json" else str(trace_path)
            self.assertEqual(status["artifacts"]["run_trace"], expected_trace)

    def test_blocked_stage_records_failure_category(self) -> None:
        prepared = prepare_run(
            question="example search question",
            runs_dir=self.temp_runs_dir(),
            angles=["primary source discovery"],
        )
        run_dir = Path(prepared["run_dir"])

        result = fetch_claims(run=run_dir)

        self.assertEqual(result["status"], "blocked_missing_fetch_queue")
        records = read_trace_records(run_dir / "run_trace.jsonl")
        self.assertEqual(records[-1]["stage"], "fetch_claims")
        self.assertEqual(records[-1]["status"], "blocked_missing_fetch_queue")
        self.assertEqual(records[-1]["failure_category"], "missing_fetch_queue")
        self.assertTrue(validate_trace_file(run_dir / "run_trace.jsonl").valid)

    def test_cli_nonzero_stage_failure_appends_trace_when_run_exists(self) -> None:
        runs_dir = self.temp_runs_dir()
        run_dir = runs_dir / "empty-run"
        run_dir.mkdir()

        command = subprocess.run(
            [str(RUNNER), "fetch-claims", "--run", str(run_dir)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(command.returncode, 2)
        records = read_trace_records(run_dir / "run_trace.jsonl")
        self.assertEqual(records[-1]["stage"], "fetch_claims")
        self.assertEqual(records[-1]["status"], "failed")
        self.assertEqual(records[-1]["failure_category"], "missing_artifact")
        self.assertTrue(validate_trace_file(run_dir / "run_trace.jsonl").valid)

    def test_cli_invalid_json_failures_are_classified_as_invalid_json(self) -> None:
        cases = [
            (
                "fetch_claims",
                "fetch-claims",
                "fetch_queue.json",
                ["fetch-claims"],
            ),
            (
                "ingest_vision",
                "ingest-vision",
                "visual_observations.jsonl",
                ["ingest-vision", "--provider", "codex-interactive"],
            ),
            (
                "synthesize",
                "synthesize",
                "evidence.json",
                ["synthesize"],
            ),
        ]
        for stage, question_suffix, corrupt_file, command_args in cases:
            with self.subTest(stage=stage):
                prepared = prepare_run(
                    question=f"invalid JSON trace {question_suffix}",
                    runs_dir=self.temp_runs_dir(),
                    route="visual_required" if stage == "ingest_vision" else "text_only",
                    angles=["primary source discovery"],
                )
                run_dir = Path(prepared["run_dir"])
                (run_dir / corrupt_file).write_text("{not valid json\n", encoding="utf-8")

                command = subprocess.run(
                    [str(RUNNER), *command_args, "--run", str(run_dir)],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )

                self.assertEqual(command.returncode, 2, command.stderr)
                records = read_trace_records(run_dir / "run_trace.jsonl")
                self.assertEqual(records[-1]["stage"], stage)
                self.assertEqual(records[-1]["status"], "failed")
                self.assertEqual(records[-1]["failure_category"], "invalid_json")
                self.assertTrue(validate_trace_file(run_dir / "run_trace.jsonl").valid)

    def test_trace_schema_validation_reports_missing_required_fields(self) -> None:
        result = validate_trace_record({"schema_version": "codex-deepresearch.run-trace.v0"})

        self.assertFalse(result.valid)
        codes = {error.code for error in result.errors}
        self.assertIn("missing_string", codes)
        self.assertIn("expected_artifacts_object", codes)


if __name__ == "__main__":
    unittest.main()
