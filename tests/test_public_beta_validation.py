from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "plugins" / "codex-deepresearch" / "scripts" / "codex-deepresearch"
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch.public_beta_validation import (  # noqa: E402
    DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST,
    PublicBetaValidationError,
    load_public_beta_prompt_manifest,
    run_public_beta_validation,
)


class PublicBetaValidationTests(unittest.TestCase):
    def temp_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def read_json(self, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def test_prompt_manifest_covers_public_safe_real_use_set(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompts = manifest["prompts"]
        visual_prompts = [
            prompt for prompt in prompts if prompt["route"] in {"visual_required", "visual_optional"}
        ]

        self.assertGreaterEqual(len(prompts), 20)
        self.assertGreaterEqual(len(visual_prompts), 8)
        self.assertTrue(all(prompt["public_safe"] is True for prompt in prompts))
        self.assertTrue(
            all("fresh_session_full_runner_artifact_handoff" in prompt["gate_tags"] for prompt in prompts if prompt["route"] == "text_only")
        )
        self.assertTrue(
            all("automatic_web_visual_e2e" in prompt["gate_tags"] for prompt in visual_prompts)
        )

    def test_default_suite_records_blocked_runs_separately_from_failures(self) -> None:
        with self.assertRaises(PublicBetaValidationError) as raised:
            run_public_beta_validation(
                runs_dir=self.temp_dir(),
                suite_id="public-beta",
                clean=True,
            )

        payload = self.read_json(raised.exception.results_path)
        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["release_gate_ready"])
        self.assertEqual(payload["prompt_coverage"]["total_prompts"], 20)
        self.assertEqual(payload["prompt_coverage"]["visual_prompts"], 10)
        self.assertEqual(
            payload["outcome_counts"],
            {"blocked": 20, "excluded": 0, "failed": 0, "passed": 0},
        )
        self.assertEqual(payload["classification_counts"]["excluded_blocked"], 20)
        self.assertTrue(all(payload["acceptance"].values()), payload["acceptance"])
        self.assertTrue(Path(payload["artifacts"]["summary"]).is_file())
        self.assertTrue(
            all(run["failure_category"] for run in payload["runs"] if run["status"] != "passed")
        )

        visual = {run["id"]: run for run in payload["runs"]}["pb-visual-001"]
        self.assertEqual(visual["terminal_status"], "blocked_missing_visual_provider")
        self.assertIn("visual_provider_status", visual["status_artifacts"])
        self.assertTrue(Path(visual["status_artifacts"]["visual_provider_status"]).is_file())

    def test_supplied_runs_count_pass_fail_and_blocked_metric_buckets(self) -> None:
        runs_dir = self.temp_dir()
        passing_run = self.write_text_run(runs_dir / "passing", status="completed_parallel")
        failed_run = self.write_text_run(runs_dir / "failed", status="failed_synthesis", ok=False)

        with self.assertRaises(PublicBetaValidationError) as raised:
            run_public_beta_validation(
                runs_dir=self.temp_dir(),
                suite_id="public-beta-supplied",
                clean=True,
                prompt_runs={
                    "pb-text-001": passing_run,
                    "pb-text-002": failed_run,
                },
            )

        payload = self.read_json(raised.exception.results_path)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["outcome_counts"]["passed"], 1)
        self.assertEqual(payload["outcome_counts"]["failed"], 1)
        self.assertEqual(payload["outcome_counts"]["blocked"], 18)
        self.assertEqual(
            payload["failure_category_counts"]["synthesis_shape_failure"],
            1,
        )
        metric = payload["prompt_metrics"]["fresh_session_full_runner_artifact_handoff"]
        self.assertEqual(metric["passed"], 1)
        self.assertEqual(metric["failed_non_blocked"], 1)
        self.assertEqual(metric["blocked"], 8)
        self.assertEqual(metric["denominator_completed_non_blocked"], 2)
        self.assertEqual(metric["pass_rate"], 0.5)

    def test_cli_allow_blocked_outputs_sanitized_results_and_exits_zero(self) -> None:
        runs_dir = self.temp_dir()
        command = subprocess.run(
            [
                str(RUNNER),
                "public-beta-validation",
                "--runs-dir",
                str(runs_dir),
                "--suite-id",
                "cli-public-beta",
                "--clean",
                "--allow-blocked",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(command.returncode, 0, command.stderr)
        payload = json.loads(command.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["raw_run_bundles_copied"])
        self.assertTrue(Path(payload["artifacts"]["results"]).is_file())
        self.assertTrue(Path(payload["artifacts"]["summary"]).is_file())

    def write_text_run(self, run_dir: Path, *, status: str, ok: bool = True) -> Path:
        run_dir.mkdir(parents=True)
        terminal = True
        self.write_json(
            run_dir / "run_status.json",
            {
                "schema_version": "codex-deepresearch.run-status.v0",
                "status": status,
                "ok": ok,
                "terminal": terminal,
                "selected_mode": "codex-plugin",
                "adapter": "codex-exec",
            },
        )
        if status.startswith("completed"):
            self.write_json(
                run_dir / "evidence.json",
                {"schema_version": "0.1.0", "mode": "codex-plugin"},
            )
            self.write_json(
                run_dir / "report_status.json",
                {"schema_version": "codex-deepresearch.report-status.v0", "used_images": []},
            )
            (run_dir / "report.md").write_text("# Public-safe report\n", encoding="utf-8")
        return run_dir


if __name__ == "__main__":
    unittest.main()
