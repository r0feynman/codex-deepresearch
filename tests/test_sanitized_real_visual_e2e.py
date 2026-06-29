from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch.sanitized_real_visual_e2e import (  # noqa: E402
    run_sanitized_real_visual_e2e,
)


class SanitizedRealVisualE2ETests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def test_sanitized_real_visual_e2e_reaches_completed_auto_visual(self) -> None:
        result = run_sanitized_real_visual_e2e(
            runs_dir=self.temp_runs_dir(),
            suite_id="sanitized-real",
            clean=True,
            scenario_timeout_seconds=120.0,
        )

        self.assertEqual(result["status"], "passed", result.get("failures"))
        self.assertTrue(result["release_gate_passed"])
        self.assertEqual(result["release_gate_status"], "passed")
        self.assertTrue(result["no_user_image"])
        self.assertTrue(result["sanitized_real_artifact"])
        self.assertFalse(result["live_web_fetch"])
        self.assertFalse(result["live_codex_vlm_session"])
        self.assertTrue(result["deterministic_codex_interactive_test_double"])
        self.assertEqual(result["counts"]["candidate_count"], 10)
        self.assertGreaterEqual(result["counts"]["fetched_artifacts"], 3)
        self.assertGreaterEqual(result["counts"]["codex_interactive_observations"], 3)
        self.assertGreaterEqual(
            result["counts"]["report_cited_visual_or_mixed_claims"],
            1,
        )
        self.assertTrue(result["visual_minimums"]["satisfied"])
        self.assertTrue(
            result["lineage"]["non_fixture_non_manual_non_user_provided"],
            result["lineage"],
        )

        provider_status_path = Path(result["artifacts"]["visual_provider_status"])
        provider_status = json.loads(provider_status_path.read_text(encoding="utf-8"))
        self.assertEqual(provider_status["status"], "completed_auto_visual")
        self.assertTrue(provider_status["minimums"]["satisfied"])
        self.assertEqual(provider_status["minimums"]["candidate_count"], 10)
        self.assertGreaterEqual(provider_status["minimums"]["fetched_artifacts"], 3)
        self.assertGreaterEqual(provider_status["minimums"]["vlm_images_analyzed"], 3)
        self.assertGreaterEqual(provider_status["minimums"]["report_cited_images"], 1)

        run_status = json.loads(
            Path(result["artifacts"]["run_status"]).read_text(encoding="utf-8")
        )
        self.assertEqual(run_status["status"], "completed_auto_visual")
        self.assertFalse(run_status["provenance"]["fixture_only"])
        self.assertFalse(run_status["provenance"]["manual_handoff"])
        self.assertFalse(run_status["provenance"]["user_provided_images"])


if __name__ == "__main__":
    unittest.main()
