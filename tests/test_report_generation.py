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

from deepresearch import synthesize_report


class ReportGenerationTests(unittest.TestCase):
    def temp_run(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        run_dir = Path(temp_dir.name) / "report-run"
        run_dir.mkdir()
        self.write_json(run_dir / "evidence.json", self.base_evidence())
        return run_dir

    def write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def load_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def base_evidence(self) -> dict:
        return {
            "schema_version": "0.1.0",
            "run_id": "report-run",
            "created_at": "2026-06-22T00:00:00Z",
            "question": "Report generation test",
            "mode": "codex-plugin",
            "search_provider": "codex-native",
            "vlm_provider": "codex-interactive",
            "routing": [],
            "search_tasks": [],
            "sources": [self.source()],
            "images": [],
            "claims": [],
        }

    def source(self, source_id: str = "src_001") -> dict:
        return {
            "id": source_id,
            "type": "web",
            "url": "https://example.com/source",
            "title": "Example Source",
            "published_at": None,
            "accessed_at": "2026-06-22T00:00:00Z",
            "quality": "primary",
            "retrieval_status": "fetched",
            "local_artifact_path": f"sources/{source_id}.html",
            "license_policy": "allowed",
            "robots_policy": "allowed",
            "policy_decision": "allowed",
            "policy_flags": [],
        }

    def image(self, image_id: str = "img_001") -> dict:
        return {
            "id": image_id,
            "source_id": "src_001",
            "origin": "screenshot",
            "image_url": "https://example.com/image.png",
            "page_url": "https://example.com/source",
            "local_artifact_path": f"images/{image_id}.png",
            "mime_type": "image/png",
            "width": 640,
            "height": 480,
            "hash": "sha256:test",
            "phash": None,
            "ocr_text": "Visible Example text",
            "observations": ["Visible Example text is present."],
            "inferences": ["The image supports the visual claim."],
            "visual_tasks": ["image_claim_alignment"],
            "analysis_provider": "codex-interactive",
            "analysis_status": "analyzed",
            "policy_flags": [],
            "caveats": [],
        }

    def claim(
        self,
        *,
        claim_id: str,
        text: str = "The source says Example.",
        claim_type: str = "text",
        supporting_sources: list[str] | None = None,
        supporting_images: list[str] | None = None,
        quote_spans: list[dict] | None = None,
        verification_status: str = "supported",
        review_status: str = "auto_reviewed",
        promotion_status: str = "eligible",
        confidence: str = "high",
        include_in_final_report: bool | None = True,
    ) -> dict:
        claim = {
            "id": claim_id,
            "text": text,
            "claim_type": claim_type,
            "supporting_sources": supporting_sources if supporting_sources is not None else ["src_001"],
            "supporting_images": supporting_images if supporting_images is not None else [],
            "quote_spans": quote_spans
            if quote_spans is not None
            else [
                {
                    "source_id": "src_001",
                    "quote": "Example",
                    "location": "paragraph 1",
                }
            ],
            "votes": [],
            "verification_status": verification_status,
            "review_status": review_status,
            "promotion_status": promotion_status,
            "confidence": confidence,
            "caveats": [],
        }
        if include_in_final_report is not None:
            claim["include_in_final_report"] = include_in_final_report
        return claim

    def test_high_confidence_text_claim_gets_source_quote_citation(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [self.claim(claim_id="claim_text")]
        self.write_json(run_dir / "evidence.json", evidence)

        status = synthesize_report(run=run_dir)

        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["claims_included"], 1)
        self.assertIn("claim `claim_text`", report)
        self.assertIn("[src_001]", report)
        self.assertIn('Quote [src_001]: "Example"', report)
        self.assertIn("https://example.com/source", report)

    def test_visual_claim_gets_image_appendix_and_evidence_id(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["images"] = [self.image()]
        evidence["claims"] = [
            self.claim(
                claim_id="claim_visual",
                text="The image shows Visible Example text.",
                claim_type="visual",
                supporting_images=["img_001"],
                quote_spans=[],
            )
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        status = synthesize_report(run=run_dir)

        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertEqual(status["used_images"], ["img_001"])
        self.assertIn("## Image Appendix", report)
        self.assertIn("Image `img_001`", report)
        self.assertIn("claim `claim_visual`", report)
        self.assertIn("Images: `img_001`", report)

    def test_unsupported_refuted_and_policy_blocked_claims_are_excluded(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [
            self.claim(
                claim_id="claim_supported",
                text="Supported claim remains.",
            ),
            self.claim(
                claim_id="claim_unsupported",
                text="Unsupported claim must not be a finding.",
                verification_status="insufficient_evidence",
                review_status="needs_more_evidence",
                promotion_status="not_eligible",
                confidence="low",
                include_in_final_report=False,
            ),
            self.claim(
                claim_id="claim_refuted",
                text="Refuted claim must not be a finding.",
                verification_status="refuted",
                promotion_status="not_eligible",
                confidence="low",
                include_in_final_report=False,
            ),
            self.claim(
                claim_id="claim_policy_blocked",
                text="Policy-blocked claim must not be a finding.",
                verification_status="policy_blocked",
                review_status="needs_more_evidence",
                promotion_status="not_eligible",
                confidence="low",
                include_in_final_report=False,
            ),
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        status = synthesize_report(run=run_dir)

        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertEqual(status["claims_included"], 1)
        self.assertEqual(status["claims_excluded"], 3)
        self.assertIn("Supported claim remains.", report)
        evidence_section = report.split("## Excluded Or Caveated Evidence")[0]
        self.assertNotIn("Unsupported claim must not be a finding.", evidence_section)
        self.assertNotIn("Refuted claim must not be a finding.", evidence_section)
        self.assertNotIn("Policy-blocked claim must not be a finding.", evidence_section)
        excluded_ids = {item["claim_id"] for item in status["excluded_claims"]}
        self.assertEqual(
            excluded_ids,
            {"claim_unsupported", "claim_refuted", "claim_policy_blocked"},
        )

    def test_cli_synthesize_writes_report_status(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [self.claim(claim_id="claim_cli")]
        self.write_json(run_dir / "evidence.json", evidence)

        command = subprocess.run(
            [str(RUNNER), "synthesize", "--run", str(run_dir)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(command.returncode, 0, command.stderr)
        payload = json.loads(command.stdout)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["claims_included"], 1)
        self.assertTrue((run_dir / "report.md").exists())
        self.assertTrue((run_dir / "report_status.json").exists())

    def test_invalid_evidence_cli_fails_without_confident_report(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["sources"][0].pop("url")
        evidence["claims"] = [
            self.claim(
                claim_id="claim_invalid_supported",
                text="Invalid evidence must not become report prose.",
            )
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        command = subprocess.run(
            [str(RUNNER), "synthesize", "--run", str(run_dir)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(command.returncode, 0)
        payload = json.loads(command.stdout)
        self.assertEqual(payload["status"], "failed_validation")
        self.assertFalse((run_dir / "report.md").exists())
        status = self.load_json(run_dir / "report_status.json")
        self.assertEqual(status["status"], "failed_validation")
        self.assertFalse(status["validation"]["valid"])
        self.assertGreater(len(status["validation"]["errors"]), 0)
        self.assertEqual(status["claims_included"], 0)
        self.assertNotIn("report", status["artifacts"])

    def test_cli_synthesize_is_byte_stable_for_unchanged_evidence(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [self.claim(claim_id="claim_stable")]
        self.write_json(run_dir / "evidence.json", evidence)

        first = subprocess.run(
            [str(RUNNER), "synthesize", "--run", str(run_dir)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        first_report = (run_dir / "report.md").read_bytes()
        first_status = (run_dir / "report_status.json").read_bytes()
        second = subprocess.run(
            [str(RUNNER), "synthesize", "--run", str(run_dir)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        second_report = (run_dir / "report.md").read_bytes()
        second_status = (run_dir / "report_status.json").read_bytes()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first_report, second_report)
        self.assertEqual(first_status, second_status)
        self.assertIn("2026-06-22T00:00:00Z", first_report.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
