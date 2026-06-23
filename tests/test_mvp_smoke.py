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

from deepresearch import run_mvp_smoke


class MvpSmokeTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def read_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_mvp_smoke_generates_release_gate_fixtures(self) -> None:
        result = run_mvp_smoke(
            runs_dir=self.temp_runs_dir(),
            suite_id="suite",
            clean=True,
            invoke="$deep-research: deterministic MVP smoke",
        )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["totals"]["text_only"], 3)
        self.assertEqual(result["totals"]["visual_required"], 3)
        self.assertEqual(result["totals"]["visual_optional"], 2)
        self.assertTrue(all(result["acceptance"].values()))
        self.assertEqual(result["install_update_smoke"]["status"], "passed")

        text_invocation = result["fixtures"]["text_only"][0]
        text_run = Path(text_invocation["run_dir"])
        self.assertTrue((text_run / "evidence.json").is_file())
        self.assertTrue((text_run / "report.md").is_file())
        text_evidence = self.read_json(text_run / "evidence.json")
        self.assertEqual(text_evidence["routing"][0]["modality"], "text_only")
        self.assertEqual(text_evidence["images"], [])
        self.assertEqual(self.read_jsonl(text_run / "visual_observations.jsonl"), [])

        visual_required = result["fixtures"]["visual_required"][0]
        visual_required_run = Path(visual_required["run_dir"])
        visual_votes = [
            vote
            for vote in self.read_jsonl(visual_required_run / "verifier_votes.jsonl")
            if vote["verifier_type"] == "visual"
        ]
        self.assertGreaterEqual(len(visual_votes), 1)
        self.assertEqual(visual_required["vision_adapter_status"], "visual_evidence_ingested")

        optional_pruned = result["fixtures"]["visual_optional"][1]
        optional_run = Path(optional_pruned["run_dir"])
        optional_evidence = self.read_json(optional_run / "evidence.json")
        self.assertEqual(optional_pruned["optional_visual_mode"], "budget_pruned_no_visual")
        self.assertEqual(optional_evidence["claims"][0]["verification_status"], "budget_pruned")
        self.assertEqual(self.read_jsonl(optional_run / "verifier_votes.jsonl"), [])

    def test_cli_mvp_smoke_outputs_machine_readable_results(self) -> None:
        runs_dir = self.temp_runs_dir()
        command = subprocess.run(
            [
                str(RUNNER),
                "mvp-smoke",
                "--runs-dir",
                str(runs_dir),
                "--suite-id",
                "cli-suite",
                "--clean",
                "--invoke",
                "$deep-research: CLI MVP smoke",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(command.returncode, 0, command.stderr)
        payload = json.loads(command.stdout)
        self.assertEqual(payload["status"], "passed")
        results_path = Path(payload["artifacts"]["results"])
        self.assertTrue(results_path.is_file())
        persisted = self.read_json(results_path)
        self.assertEqual(persisted["schema_version"], "codex-deepresearch.mvp-smoke.v0")
        self.assertEqual(persisted["totals"]["run_artifacts"], 8)


if __name__ == "__main__":
    unittest.main()
