from __future__ import annotations

import importlib.util
import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATE_REPO = ROOT / "scripts" / "validate_repo.py"


def load_validate_repo_module():
    spec = importlib.util.spec_from_file_location("validate_repo", VALIDATE_REPO)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load validate_repo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validate_repo = load_validate_repo_module()


class ValidateRepoMvpSmokeResultTests(unittest.TestCase):
    def passing_payload(self) -> dict:
        return {
            "status": "passed",
            "totals": {
                "text_only": 3,
                "visual_required": 3,
                "visual_optional": 2,
                "failed_fixtures": 0,
            },
            "install_update_smoke": {"status": "passed"},
            "guardrail_fixture_suite": {"status": "passed"},
            "skips": {},
            "acceptance": {
                "deep_research_invocation_valid": True,
                "deep_research_invocation_completes_text_only_run": True,
                "plugin_install_update_smoke_passes": True,
                "text_only_zero_vlm_calls": True,
                "visual_required_handoff_and_visual_verifier": True,
                "evidence_validates_schema_v0": True,
                "guardrail_fixture_suite_passes": True,
                "fixture_counts_match_mvp_gate": True,
                "visual_optional_budget_pruned_path": True,
            },
        }

    def skipped_install_payload(self) -> dict:
        payload = self.passing_payload()
        payload["status"] = "failed"
        payload["install_update_smoke"] = {"status": "skipped"}
        payload["skips"] = {"codex_cli_install_check": True}
        payload["acceptance"] = dict(payload["acceptance"])
        payload["acceptance"]["plugin_install_update_smoke_passes"] = False
        return payload

    def test_full_mvp_smoke_pass_required_when_codex_cli_available(self) -> None:
        validate_repo.validate_mvp_smoke_result(
            self.passing_payload(),
            codex_cli_available=True,
        )

    def test_ci_without_codex_accepts_honest_install_skip_only(self) -> None:
        validate_repo.validate_mvp_smoke_result(
            self.skipped_install_payload(),
            codex_cli_available=False,
        )

    def test_ci_without_codex_rejects_skipped_fixture_failure(self) -> None:
        payload = self.skipped_install_payload()
        payload["acceptance"]["visual_required_handoff_and_visual_verifier"] = False
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                validate_repo.validate_mvp_smoke_result(
                    payload,
                    codex_cli_available=False,
                )

    def test_ci_without_codex_rejects_install_skip_reported_as_pass(self) -> None:
        payload = self.skipped_install_payload()
        payload["acceptance"]["plugin_install_update_smoke_passes"] = True
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                validate_repo.validate_mvp_smoke_result(
                    payload,
                    codex_cli_available=False,
                )


if __name__ == "__main__":
    unittest.main()
