from __future__ import annotations

import csv
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

from deepresearch import ReportExportError, export_report, synthesize_report


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

    def export_output_path(self, run_dir: Path, result: dict, export_format: str) -> Path:
        return run_dir / result["outputs"][export_format]

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
        caveats: list[str] | None = None,
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
            "caveats": caveats if caveats is not None else [],
        }
        if include_in_final_report is not None:
            claim["include_in_final_report"] = include_in_final_report
        return claim

    def visual_support(self, image_id: str = "img_001", observation_index: int = 0) -> dict:
        return {
            "image_id": image_id,
            "observation_ref": f"images.{image_id}.observations[{observation_index}]",
            "observation_index": observation_index,
            "observation_text": "Visible Example text is present.",
            "relation_type": "screenshot_support",
            "provider": "codex-interactive",
            "rationale": "Linked because claim and image cite source_id 'src_001'.",
            "confidence": 0.74,
        }

    def report_export_run(self) -> Path:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["question"] = "Create a market report with competitor and incident evidence."
        evidence["images"] = [self.image()]
        evidence["claims"] = [
            self.claim(
                claim_id="claim_supported",
                text="Supported technical finding remains reportable.",
                caveats=["Scope is limited to fixture evidence."],
            ),
            self.claim(
                claim_id="claim_visual",
                text="The image shows Visible Example text.",
                claim_type="visual",
                supporting_images=["img_001"],
                quote_spans=[],
            ),
            self.claim(
                claim_id="claim_supported_not_eligible_export",
                text="Supported but not eligible claim must stay out of default exports.",
                promotion_status="not_eligible",
            ),
            self.claim(
                claim_id="claim_rejected_high_confidence",
                text="Rejected high-confidence claim must not appear as a finding.",
                verification_status="refuted",
                promotion_status="not_eligible",
                confidence="high",
                include_in_final_report=False,
            ),
            self.claim(
                claim_id="claim_policy_blocked_export",
                text="Policy-blocked claim must stay out of default exports.",
                verification_status="policy_blocked",
                review_status="needs_more_evidence",
                promotion_status="not_eligible",
                confidence="low",
                include_in_final_report=False,
            ),
            self.claim(
                claim_id="claim_not_eligible_export",
                text="Not eligible claim must stay out of default exports.",
                verification_status="insufficient_evidence",
                review_status="needs_more_evidence",
                promotion_status="not_eligible",
                confidence="low",
                include_in_final_report=False,
            ),
        ]
        evidence["claims"][1]["visual_supports"] = [self.visual_support()]
        evidence["claims"][1]["verifier_vote_refs"] = ["vote_text_1", "vote_visual_1", "vote_policy_1"]
        evidence["claims"][1]["visual_verifier_vote_refs"] = ["vote_visual_1"]
        self.write_json(run_dir / "evidence.json", evidence)
        status = synthesize_report(run=run_dir)
        self.assertEqual(status["status"], "completed")
        return run_dir

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
        evidence["claims"][0]["visual_supports"] = [self.visual_support()]
        self.write_json(run_dir / "evidence.json", evidence)

        status = synthesize_report(run=run_dir)

        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertEqual(status["used_images"], ["img_001"])
        self.assertIn("## Visual Findings", report)
        self.assertIn("## Image Appendix", report)
        self.assertIn("provider `codex-interactive`", report)
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
        evidence_section = report.split("## Conflicts")[0]
        self.assertNotIn("Unsupported claim must not be a finding.", evidence_section)
        self.assertNotIn("Refuted claim must not be a finding.", evidence_section)
        self.assertNotIn("Policy-blocked claim must not be a finding.", evidence_section)
        self.assertIn("## Conflicts", report)
        self.assertIn("claim_refuted", report)
        excluded_ids = {item["claim_id"] for item in status["excluded_claims"]}
        self.assertEqual(
            excluded_ids,
            {"claim_unsupported", "claim_refuted", "claim_policy_blocked"},
        )

    def test_supported_not_eligible_claim_is_excluded_from_synthesis_and_default_exports(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        not_eligible_source = self.source("src_not_eligible")
        not_eligible_source["url"] = "https://example.com/not-eligible"
        not_eligible_image = self.image("img_not_eligible")
        not_eligible_image["source_id"] = "src_not_eligible"
        not_eligible_image["image_url"] = "https://example.com/not-eligible.png"
        not_eligible_image["page_url"] = "https://example.com/not-eligible"
        not_eligible_text = "Supported auto-reviewed high confidence not eligible claim must not be reported."
        evidence["sources"].append(not_eligible_source)
        evidence["images"] = [not_eligible_image]
        evidence["claims"] = [
            self.claim(
                claim_id="claim_reportable",
                text="Reportable supported claim remains in the report.",
            ),
            self.claim(
                claim_id="claim_supported_not_eligible",
                text=not_eligible_text,
                claim_type="mixed",
                supporting_sources=["src_not_eligible"],
                supporting_images=["img_not_eligible"],
                quote_spans=[
                    {
                        "source_id": "src_not_eligible",
                        "quote": "not eligible fixture",
                        "location": "paragraph 2",
                    }
                ],
                verification_status="supported",
                review_status="auto_reviewed",
                promotion_status="not_eligible",
                confidence="high",
            ),
        ]
        evidence["claims"][1]["visual_supports"] = [self.visual_support("img_not_eligible")]
        self.write_json(run_dir / "evidence.json", evidence)

        status = synthesize_report(run=run_dir)

        report = (run_dir / "report.md").read_text(encoding="utf-8")
        included_ids = {item["claim_id"] for item in status["included_claims"]}
        excluded = {item["claim_id"]: item for item in status["excluded_claims"]}
        self.assertEqual(status["claims_included"], 1)
        self.assertEqual(status["claims_excluded"], 1)
        self.assertEqual(status["used_sources"], ["src_001"])
        self.assertEqual(status["used_images"], [])
        self.assertEqual(included_ids, {"claim_reportable"})
        self.assertEqual(set(excluded), {"claim_supported_not_eligible"})
        self.assertIn("not_eligible", excluded["claim_supported_not_eligible"]["exclusion_reasons"])
        self.assertEqual(excluded["claim_supported_not_eligible"]["source_ids"], ["src_not_eligible"])
        self.assertEqual(excluded["claim_supported_not_eligible"]["image_ids"], ["img_not_eligible"])
        before_excluded = report.split("## Excluded Or Caveated Evidence", 1)[0]
        excluded_section = report.split("## Excluded Or Caveated Evidence", 1)[1]
        self.assertNotIn(not_eligible_text, before_excluded)
        self.assertNotIn("src_not_eligible", before_excluded)
        self.assertNotIn("img_not_eligible", before_excluded)
        self.assertIn(not_eligible_text, excluded_section)
        self.assertIn("not_eligible", excluded_section)

        default_result = export_report(
            run=run_dir,
            template="technical_report",
            formats=["json", "markdown"],
            output_dir=run_dir / "default-export",
        )
        default_payload = self.load_json(self.export_output_path(run_dir, default_result, "json"))
        default_markdown = self.export_output_path(run_dir, default_result, "markdown").read_text(encoding="utf-8")
        self.assertEqual(default_payload["claim_ids"], ["claim_reportable"])
        self.assertEqual(default_payload["used_source_ids"], ["src_001"])
        self.assertEqual(default_payload["used_image_ids"], [])
        self.assertEqual(default_payload["report_status"]["used_sources"], ["src_001"])
        self.assertEqual(default_payload["report_status"]["used_images"], [])
        self.assertEqual(default_payload["excluded_claims"], [])
        self.assertNotIn("claim_supported_not_eligible", default_markdown)
        self.assertNotIn(not_eligible_text, default_markdown)

        caveat_result = export_report(
            run=run_dir,
            template="technical_report",
            formats=["json", "markdown"],
            output_dir=run_dir / "excluded-export",
            include_excluded_caveats=True,
        )
        caveat_payload = self.load_json(self.export_output_path(run_dir, caveat_result, "json"))
        caveat_markdown = self.export_output_path(run_dir, caveat_result, "markdown").read_text(encoding="utf-8")
        self.assertEqual(
            [claim["claim_id"] for claim in caveat_payload["excluded_claims"]],
            ["claim_supported_not_eligible"],
        )
        self.assertIn("Excluded claim `claim_supported_not_eligible`", caveat_markdown)
        self.assertIn(not_eligible_text, caveat_markdown)

    def test_supported_claim_backed_by_blocked_source_is_excluded(self) -> None:
        run_dir = self.temp_run()
        claim_text = "Access-controlled source text must not become a finding."
        evidence = self.load_json(run_dir / "evidence.json")
        source = evidence["sources"][0]
        source["policy_decision"] = "blocked"
        source["retrieval_status"] = "failed"
        source["retrieval_error"] = "guardrail_blocked_access_controlled"
        source["policy_flags"] = ["login_gated"]
        evidence["claims"] = [
            self.claim(
                claim_id="claim_stale_policy_source",
                text=claim_text,
                verification_status="supported",
                review_status="auto_reviewed",
                promotion_status="eligible",
                confidence="high",
            )
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        status = synthesize_report(run=run_dir)

        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["claims_included"], 0)
        self.assertEqual(status["claims_excluded"], 1)
        self.assertEqual(status["used_sources"], [])
        evidence_section = report.split("## Excluded Or Caveated Evidence")[0]
        self.assertNotIn(claim_text, evidence_section)
        self.assertIn("No supported claims met the report evidence requirements.", report)
        self.assertIn("policy_blocked_source", status["excluded_claims"][0]["exclusion_reasons"])
        self.assertIn("policy_blocked_source", report)
        self.assertIn("claim_stale_policy_source", report)

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
        self.assertEqual(payload["run_dir"], ".")
        self.assertEqual(payload["report_path"], "report.md")
        self.assertEqual(payload["report_status_path"], "report_status.json")
        self.assertEqual(payload["artifacts"]["evidence"], "evidence.json")
        self.assertNotIn(str(run_dir), command.stdout)

    def test_synthesize_redacts_private_metadata_and_uses_relative_status_refs(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["question"] = (
            "Investigate /home/user/private/question.txt and "
            "file:///Users/alice/private/question.html"
        )
        source = evidence["sources"][0]
        source["title"] = "Private source title /Users/alice/private/source-title.txt"
        source["url"] = "file:///home/user/private/source.html"
        source["local_artifact_path"] = str(run_dir / "sources" / "src_001.html")
        image = self.image()
        image["image_url"] = "file:///home/user/private/image.png"
        image["page_url"] = "/Users/alice/private/page.html"
        image["local_artifact_path"] = str(run_dir / "images" / "img_001.png")
        image["observations"] = [
            "Observation mentions /home/user/private/observation.png and "
            "file:///home/user/private/observation.html"
        ]
        evidence["images"] = [image]
        evidence["claims"] = [
            self.claim(
                claim_id="claim_private_report",
                text="Supported claim mentions /home/user/private/claim.txt.",
                claim_type="visual",
                supporting_images=["img_001"],
                quote_spans=[
                    {
                        "source_id": "src_001",
                        "quote": "quote leaks file:///home/user/private/quote.html",
                        "location": "/Users/alice/private/location.txt",
                    }
                ],
                caveats=["Caveat includes /home/user/private/caveat.txt"],
            )
        ]
        support = self.visual_support()
        support["observation_text"] = image["observations"][0]
        evidence["claims"][0]["visual_supports"] = [support]
        self.write_json(run_dir / "evidence.json", evidence)

        status = synthesize_report(run=run_dir)

        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["run_dir"], ".")
        self.assertEqual(status["report_path"], "report.md")
        self.assertEqual(status["report_status_path"], "report_status.json")
        self.assertEqual(status["artifacts"]["evidence"], "evidence.json")
        self.assertEqual(status["artifacts"]["report"], "report.md")
        self.assertEqual(status["artifacts"]["report_status"], "report_status.json")
        self.assertEqual(status["artifacts"]["run_steps"], "run_steps.json")
        self.assertEqual(status["artifacts"]["run_trace"], "run_trace.jsonl")
        report = (run_dir / "report.md").read_text(encoding="utf-8")
        status_text = (run_dir / "report_status.json").read_text(encoding="utf-8")
        for content in (report, status_text):
            self.assertNotIn(str(run_dir), content)
            self.assertNotIn("/home/user/private", content)
            self.assertNotIn("/Users/alice/private", content)
            self.assertNotIn("file:///home/user/private", content)
        self.assertIn("images/img_001.png", report)
        self.assertIn("<redacted-file-url>", report)
        self.assertIn("<redacted-local-path>", report)

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
        self.assertEqual(status["run_dir"], ".")
        self.assertEqual(status["report_status_path"], "report_status.json")
        self.assertEqual(status["artifacts"]["evidence"], "evidence.json")
        self.assertEqual(status["artifacts"]["report_status"], "report_status.json")
        self.assertNotIn(str(run_dir), command.stdout)

    def test_failed_validation_rerun_removes_stale_confident_report(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [
            self.claim(
                claim_id="claim_stale",
                text="Previously valid high-confidence finding.",
            )
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        valid = subprocess.run(
            [str(RUNNER), "synthesize", "--run", str(run_dir)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)
        report_path = run_dir / "report.md"
        self.assertIn(
            "Previously valid high-confidence finding.",
            report_path.read_text(encoding="utf-8"),
        )

        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"][0]["supporting_sources"] = ["src_missing"]
        evidence["claims"][0]["quote_spans"][0]["source_id"] = "src_missing"
        self.write_json(run_dir / "evidence.json", evidence)
        invalid = subprocess.run(
            [str(RUNNER), "synthesize", "--run", str(run_dir)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(invalid.returncode, 0)
        payload = json.loads(invalid.stdout)
        self.assertEqual(payload["status"], "failed_validation")
        self.assertFalse(report_path.exists())
        status = self.load_json(run_dir / "report_status.json")
        self.assertEqual(status["status"], "failed_validation")
        self.assertFalse(status["validation"]["valid"])

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

    def test_korean_comparison_report_uses_korean_table_and_gap_sections(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["question"] = (
            "Claude Code deep-research식 20~100개 병렬 조사 경험을 Codex plugin에서 구현하려면 "
            "현재 가능한 자동화 범위와 제한을 표로 비교하고, 남은 gap을 알려줘."
        )
        evidence["claims"] = [
            self.claim(
                claim_id="claim_automation_scope",
                text="Codex plugin은 준비, 병렬 오케스트레이션, 검증, 합성 단계를 자동화할 수 있다.",
                quote_spans=[
                    {
                        "source_id": "src_001",
                        "quote": "병렬 오케스트레이션과 합성 단계가 실행된다.",
                        "location": "fixture paragraph 1",
                    }
                ],
            ),
            self.claim(
                claim_id="claim_limitations",
                text="제한은 Codex 실행 신뢰 디렉터리, 인증, 비용 상한, child session 정리에 있다.",
                caveats=["실사용 codex-exec 검증은 별도 gate가 필요하다."],
                quote_spans=[
                    {
                        "source_id": "src_001",
                        "quote": "신뢰 디렉터리와 비용 상한은 제한으로 남는다.",
                        "location": "fixture paragraph 2",
                    }
                ],
            ),
            self.claim(
                claim_id="claim_remaining_gap",
                text="남은 gap은 fixture 성공과 real codex-exec 성공을 구분해 검증하는 것이다.",
                quote_spans=[
                    {
                        "source_id": "src_001",
                        "quote": "fixture 성공은 real E2E acceptance를 대체하지 않는다.",
                        "location": "fixture paragraph 3",
                    }
                ],
            ),
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        status = synthesize_report(run=run_dir)

        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["report_shape"]["language"], "ko")
        self.assertTrue(status["report_shape"]["comparison"])
        self.assertIn("## 결론", report)
        answer = report.split("## 결론", 1)[1].split("## 비교표", 1)[0]
        self.assertIn("직접 답변:", answer)
        self.assertIn("Codex plugin은 준비, 병렬 오케스트레이션", answer)
        self.assertIn("다만 제한은 Codex 실행 신뢰 디렉터리", answer)
        self.assertIn("남은 gap은 fixture 성공과 real codex-exec 성공", answer)
        self.assertIn("근거:", answer)
        self.assertNotIn("확인된 근거를 기준별로 정리했습니다", answer)
        self.assertIn("## 비교표", report)
        self.assertIn("| 기준 | 확인된 내용 | 근거 | 주의점 |", report)
        self.assertIn("| 자동화 범위 |", report)
        self.assertIn("| 제한 |", report)
        self.assertIn("| 남은 gap |", report)
        self.assertIn("## 확인된 내용", report)
        self.assertIn("## 상충되는 근거", report)
        self.assertIn("## 주의점과 남은 gap", report)
        self.assertIn("## 제외 또는 낮은 신뢰도 근거", report)

    def test_korean_comparison_direct_answer_avoids_table_like_gap_claim(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["question"] = (
            "Claude Code deep-research식 20~100개 병렬 조사 경험을 Codex plugin에서 구현하려면 "
            "현재 가능한 자동화 범위와 제한을 표로 비교하고, 남은 gap을 알려줘."
        )
        evidence["claims"] = [
            self.claim(
                claim_id="claim_automation_scope",
                text="Codex plugin은 shard 계획, codex exec 실행, evidence 병합까지 자동화할 수 있다.",
            ),
            self.claim(
                claim_id="claim_limitations",
                text=(
                    "20~100개 병렬 조사 목표에 대한 decision table은 다음과 같다. "
                    "행 1: shard 실행은 가능하지만 제한은 공식 동시성 보장 부재다."
                ),
                caveats=["주요 제한은 공식 동시성 보장 부재, sandbox/approval/auth 정책, 비용과 rate limit이다."],
            ),
            self.claim(
                claim_id="claim_meta_gap_table",
                text=(
                    "의사결정용 비교표: 기능 | Claude Code deep-research식 경험 | "
                    "Codex plugin에서 현재 가능한 대응 | 남은 gap."
                ),
                caveats=["제품 수준 스케줄링·재시도·모니터링 계층은 아직 별도 구현이 필요하다."],
            ),
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        status = synthesize_report(run=run_dir)

        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertEqual(status["status"], "completed")
        answer = report.split("## 결론", 1)[1].split("## 비교표", 1)[0]
        self.assertIn("직접 답변:", answer)
        self.assertIn("shard 계획", answer)
        self.assertIn("공식 동시성 보장 부재", answer)
        self.assertIn("제품 수준 스케줄링·재시도·모니터링 계층", answer)
        self.assertNotIn("의사결정용 비교표", answer)
        self.assertNotIn("decision table", answer)
        self.assertNotIn("행 1:", answer)
        self.assertNotIn("기능 | Claude", answer)
        self.assertNotIn(" | ", answer)

    def test_technical_adoption_report_starts_with_direct_answer_and_separates_evidence(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["question"] = (
            "Python free-threading이 현재 프로덕션 도입에 적합한지 공식 문서와 "
            "주요 패키지 호환성 근거로 판단해줘."
        )
        evidence["claims"] = [
            self.claim(
                claim_id="claim_adoption_fit",
                text="Python free-threading은 전체 워크로드의 기본 프로덕션 선택지로 전환하기에는 아직 이르다.",
                caveats=["패키지 호환성은 배포 단위로 확인해야 한다."],
            ),
            self.claim(
                claim_id="claim_package_gap",
                text="Some package compatibility evidence remains incomplete.",
                verification_status="insufficient_evidence",
                review_status="needs_more_evidence",
                promotion_status="not_eligible",
                confidence="low",
                include_in_final_report=False,
            ),
            self.claim(
                claim_id="claim_refuted_package",
                text="All Python packages are already free-threading safe.",
                verification_status="refuted",
                promotion_status="not_eligible",
                confidence="low",
                include_in_final_report=False,
            ),
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        status = synthesize_report(run=run_dir)

        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["report_shape"]["language"], "ko")
        answer = report.split("## 결론", 1)[1].split("## 확인된 내용", 1)[0]
        self.assertIn("직접 답변:", answer)
        self.assertIn("전체 워크로드의 기본 프로덕션 선택지로 전환하기에는 아직 이르다", answer)
        self.assertIn("근거:", answer)
        self.assertNotIn("not yet a default production adoption choice", answer)
        self.assertIn("## 확인된 내용", report)
        confirmed = report.split("## 확인된 내용", 1)[1].split("## 상충되는 근거", 1)[0]
        self.assertIn("Python free-threading은 전체 워크로드의 기본 프로덕션 선택지", confirmed)
        self.assertIn("## 상충되는 근거", report)
        self.assertIn("claim_refuted_package", report)
        self.assertIn("## 주의점과 남은 gap", report)
        self.assertIn("claim_package_gap", report)
        self.assertIn("## 제외 또는 낮은 신뢰도 근거", report)

    def test_recommendation_direct_answer_prefers_decision_claim_over_incidental_detail(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["question"] = (
            "Python free-threading이 현재 프로덕션 도입에 적합한지 공식 문서와 "
            "주요 패키지 호환성 근거로 판단해줘."
        )
        evidence["claims"] = [
            self.claim(
                claim_id="claim_incidental_package_production_detail",
                text="프로덕션 도입 검토에는 NumPy와 같은 패키지 호환성 확인이 포함된다.",
            ),
            self.claim(
                claim_id="claim_decision_judgment",
                text="판단: Python free-threading은 일반 프로덕션 기본값으로 전면 도입하기에는 아직 이르다.",
            ),
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        status = synthesize_report(run=run_dir)

        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertEqual(status["status"], "completed")
        answer = report.split("## 결론", 1)[1].split("## 확인된 내용", 1)[0]
        self.assertIn("전면 도입하기에는 아직 이르다", answer)
        self.assertNotIn("NumPy와 같은 패키지 호환성 확인", answer)

    def test_boilerplate_supported_claim_is_downranked_from_confirmed_findings(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [
            self.claim(
                claim_id="claim_boilerplate",
                text="Skip to content navigation menu sign in privacy policy.",
            ),
            self.claim(
                claim_id="claim_footer_labels",
                text="Privacy Policy Terms of Service Contact",
            ),
            self.claim(
                claim_id="claim_real_finding",
                text="A substantive report finding remains visible.",
            ),
            self.claim(
                claim_id="claim_substantive_privacy_policy",
                text="The privacy policy now requires users to accept arbitration for some disputes.",
            ),
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        status = synthesize_report(run=run_dir)

        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertEqual(status["claims_included"], 2)
        excluded = {item["claim_id"]: item for item in status["excluded_claims"]}
        self.assertEqual(set(excluded), {"claim_boilerplate", "claim_footer_labels"})
        self.assertIn("boilerplate_noise", excluded["claim_boilerplate"]["exclusion_reasons"])
        self.assertIn("boilerplate_noise", excluded["claim_footer_labels"]["exclusion_reasons"])
        confirmed = report.split("## Confirmed Findings", 1)[1].split("## Conflicts", 1)[0]
        self.assertNotIn("Skip to content", confirmed)
        self.assertNotIn("Privacy Policy Terms of Service Contact", confirmed)
        self.assertIn("A substantive report finding remains visible.", confirmed)
        self.assertIn(
            "The privacy policy now requires users to accept arbitration for some disputes.",
            confirmed,
        )

    def test_report_export_templates_use_same_supported_evidence_model(self) -> None:
        run_dir = self.report_export_run()
        output_dir = run_dir / "template-exports"

        for template in ("technical", "market", "competitor", "incident"):
            result = export_report(
                run=run_dir,
                template=template,
                formats=["markdown"],
                output_dir=output_dir / template,
            )
            self.assertEqual(result["status"], "completed")
            markdown = self.export_output_path(run_dir, result, "markdown").read_text(encoding="utf-8")
            self.assertIn("Supported technical finding remains reportable.", markdown)
            self.assertIn("claim_visual", markdown)
            self.assertIn("Image `img_001`", markdown)
            self.assertIn("[src_001]", markdown)
            self.assertNotIn("Rejected high-confidence claim must not appear as a finding.", markdown)
            self.assertNotIn("Policy-blocked claim must stay out of default exports.", markdown)
            self.assertNotIn("Not eligible claim must stay out of default exports.", markdown)
            self.assertNotIn("Supported but not eligible claim must stay out of default exports.", markdown)

    def test_json_csv_and_html_exports_preserve_citation_and_image_linkage(self) -> None:
        run_dir = self.report_export_run()
        result = export_report(
            run=run_dir,
            template="competitor_analysis",
            formats=["all"],
            output_dir=run_dir / "all-exports",
        )

        payload = self.load_json(self.export_output_path(run_dir, result, "json"))
        self.assertEqual(payload["report_status"]["status"], "completed")
        self.assertEqual(payload["report_status"]["visual_observation_report_links_written"], 0)
        self.assertEqual(payload["claim_ids"], ["claim_supported", "claim_visual"])
        self.assertEqual(payload["used_source_ids"], ["src_001"])
        self.assertEqual(payload["used_image_ids"], ["img_001"])
        self.assertEqual(payload["caveats"], ["Scope is limited to fixture evidence."])
        self.assertEqual(payload["image_appendix"][0]["image_id"], "img_001")
        self.assertEqual(payload["image_appendix"][0]["artifact"], "images/img_001.png")
        self.assertEqual(payload["excluded_claims"], [])
        visual_claim = payload["claims"][1]
        self.assertEqual(
            visual_claim["visual_supports"][0]["observation_ref"],
            "images.img_001.observations[0]",
        )
        self.assertEqual(visual_claim["visual_supports"][0]["observation_index"], 0)
        self.assertEqual(
            visual_claim["verifier_vote_refs"],
            ["vote_text_1", "vote_visual_1", "vote_policy_1"],
        )
        self.assertEqual(visual_claim["visual_verifier_vote_refs"], ["vote_visual_1"])
        visual_status_claim = {
            claim["claim_id"]: claim for claim in payload["report_status"]["included_claims"]
        }["claim_visual"]
        self.assertEqual(
            visual_status_claim["visual_supports"][0]["observation_ref"],
            "images.img_001.observations[0]",
        )
        self.assertEqual(visual_status_claim["visual_supports"][0]["observation_index"], 0)
        self.assertEqual(
            visual_status_claim["verifier_vote_refs"],
            ["vote_text_1", "vote_visual_1", "vote_policy_1"],
        )
        self.assertEqual(visual_status_claim["visual_verifier_vote_refs"], ["vote_visual_1"])

        with self.export_output_path(run_dir, result, "csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["claim_id"] for row in rows], ["claim_supported", "claim_visual"])
        self.assertEqual(rows[0]["source_ids"], "src_001")
        self.assertEqual(rows[1]["image_ids"], "img_001")
        self.assertEqual(rows[0]["quote_source_ids"], "src_001")
        self.assertEqual(rows[1]["visual_support_refs"], "images.img_001.observations[0]")
        self.assertEqual(rows[1]["verifier_vote_refs"], "vote_text_1;vote_visual_1;vote_policy_1")
        self.assertEqual(rows[1]["visual_verifier_vote_refs"], "vote_visual_1")

        html_report = self.export_output_path(run_dir, result, "html").read_text(encoding="utf-8")
        html_manifest = self.export_output_path(run_dir, result, "html").with_name("manifest.json")
        self.assertTrue(html_manifest.exists())
        self.assertIn("[src_001]", html_report)
        self.assertIn("Image <code>img_001</code>", html_report)
        self.assertIn("Supported technical finding remains reportable.", html_report)

    def test_report_exports_redact_private_file_urls_and_absolute_paths(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["question"] = (
            "Export report for /home/user/private/question.txt and "
            "file:///Users/alice/private/question.html"
        )
        private_source = self.source("src_private")
        private_source["title"] = "Private title /Users/alice/private/source-title.txt"
        private_source["url"] = "file:///home/user/private/source.html"
        private_source["local_artifact_path"] = str(run_dir / "sources" / "src_private.html")
        private_image = self.image("img_private")
        private_image["source_id"] = "src_private"
        private_image["image_url"] = "/home/user/private/image.png"
        private_image["page_url"] = "file:///home/user/private/page.html"
        private_image["local_artifact_path"] = str(run_dir / "images" / "img_private.png")
        private_image["observations"] = [
            "Observation leaks /home/user/private/observation.png and "
            "file:///home/user/private/observation.html"
        ]
        private_image["caveats"] = ["Image caveat leaks /Users/alice/private/image-caveat.txt"]
        evidence["sources"].append(private_source)
        evidence["images"] = [private_image]
        evidence["claims"] = [
            self.claim(
                claim_id="claim_public_source",
                text="Public source URL should be preserved.",
            ),
            self.claim(
                claim_id="claim_private_source",
                text="Private source URL /home/user/private/claim.txt should be redacted in exports.",
                supporting_sources=["src_private"],
                quote_spans=[
                    {
                        "source_id": "src_private",
                        "quote": "private source fixture file:///home/user/private/quote.html",
                        "location": "/Users/alice/private/location.txt",
                    }
                ],
            ),
            self.claim(
                claim_id="claim_private_image",
                text="Private image URLs should be redacted in exports.",
                claim_type="visual",
                supporting_sources=["src_private"],
                supporting_images=["img_private"],
                quote_spans=[],
                caveats=["Claim caveat leaks /home/user/private/claim-caveat.txt"],
            ),
        ]
        support = self.visual_support("img_private")
        support["observation_text"] = private_image["observations"][0]
        evidence["claims"][2]["visual_supports"] = [support]
        self.write_json(run_dir / "evidence.json", evidence)
        status = synthesize_report(run=run_dir)
        self.assertEqual(status["status"], "completed")

        result = export_report(
            run=run_dir,
            template="market_report",
            formats=["all"],
            output_dir=run_dir / "private-redaction-export",
        )

        payload = self.load_json(self.export_output_path(run_dir, result, "json"))
        sources = {source["source_id"]: source for source in payload["sources"]}
        image = payload["image_appendix"][0]
        self.assertIn("<redacted-local-path>", payload["question"])
        self.assertIn("<redacted-file-url>", payload["question"])
        self.assertEqual(sources["src_001"]["url"], "https://example.com/source")
        self.assertIn("<redacted-local-path>", sources["src_private"]["title"])
        self.assertEqual(sources["src_private"]["url"], "<redacted-file-url>")
        self.assertEqual(sources["src_private"]["artifact"], "sources/src_private.html")
        self.assertEqual(image["image_url"], "<redacted-local-path>")
        self.assertEqual(image["page_url"], "<redacted-file-url>")
        self.assertEqual(image["artifact"], "images/img_private.png")
        self.assertIn("<redacted-local-path>", image["observations"][0])
        self.assertIn("<redacted-file-url>", image["observations"][0])
        self.assertIn("<redacted-local-path>", image["caveats"][0])
        self.assertEqual(result["output_dir"], "private-redaction-export")
        self.assertNotIn(str(run_dir), json.dumps(result, sort_keys=True))

        output_paths = [run_dir / path for path in result["outputs"].values()]
        output_paths.append(self.export_output_path(run_dir, result, "html").with_name("manifest.json"))
        for output_path in output_paths:
            content = output_path.read_text(encoding="utf-8")
            self.assertNotIn(str(run_dir), content, output_path)
            self.assertNotIn("/home/user/private", content, output_path)
            self.assertNotIn("/Users/alice/private", content, output_path)
            self.assertNotIn("file:///home/user/private", content, output_path)
        self.assertIn(
            "https://example.com/source",
            self.export_output_path(run_dir, result, "markdown").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "https://example.com/source",
            self.export_output_path(run_dir, result, "html").read_text(encoding="utf-8"),
        )

    def test_export_include_excluded_caveats_marks_non_reportable_claims(self) -> None:
        run_dir = self.report_export_run()

        default_result = export_report(
            run=run_dir,
            template="incident_report",
            formats=["markdown"],
            output_dir=run_dir / "default-export",
        )
        default_markdown = self.export_output_path(run_dir, default_result, "markdown").read_text(encoding="utf-8")
        self.assertNotIn("Excluded Or Caveated Claims", default_markdown)
        self.assertNotIn("claim_rejected_high_confidence", default_markdown)
        self.assertNotIn("claim_supported_not_eligible_export", default_markdown)

        included_result = export_report(
            run=run_dir,
            template="incident_report",
            formats=["json", "csv", "markdown"],
            output_dir=run_dir / "excluded-export",
            include_excluded_caveats=True,
        )
        markdown = self.export_output_path(run_dir, included_result, "markdown").read_text(encoding="utf-8")
        self.assertIn("## Excluded Or Caveated Claims", markdown)
        self.assertIn("Excluded claim `claim_rejected_high_confidence`", markdown)
        self.assertIn("status `refuted`", markdown)
        self.assertIn("Excluded claim `claim_supported_not_eligible_export`", markdown)
        self.assertIn("Exclusion reasons: not_eligible", markdown)
        self.assertIn("Excluded claim `claim_policy_blocked_export`", markdown)
        self.assertIn("Excluded claim `claim_not_eligible_export`", markdown)

        payload = self.load_json(self.export_output_path(run_dir, included_result, "json"))
        excluded_ids = {claim["claim_id"] for claim in payload["excluded_claims"]}
        self.assertEqual(
            excluded_ids,
            {
                "claim_supported_not_eligible_export",
                "claim_rejected_high_confidence",
                "claim_policy_blocked_export",
                "claim_not_eligible_export",
            },
        )
        with self.export_output_path(run_dir, included_result, "csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        excluded_rows = [row for row in rows if row["row_type"] == "excluded_caveat"]
        self.assertEqual(len(excluded_rows), 4)

    def test_cli_export_report_writes_all_formats(self) -> None:
        run_dir = self.report_export_run()
        output_dir = run_dir / "cli-exports"

        command = subprocess.run(
            [
                str(RUNNER),
                "export-report",
                "--run",
                str(run_dir),
                "--template",
                "technical_report",
                "--format",
                "all",
                "--output-dir",
                str(output_dir),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(command.returncode, 0, command.stderr)
        payload = json.loads(command.stdout)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["template"], "technical_report")
        self.assertEqual(payload["output_dir"], "cli-exports")
        self.assertEqual(
            set(payload["outputs"]),
            {"markdown", "json", "csv", "html"},
        )
        self.assertEqual(payload["outputs"]["markdown"], "cli-exports/technical_report.md")
        self.assertEqual(payload["outputs"]["json"], "cli-exports/technical_report.json")
        self.assertEqual(payload["outputs"]["csv"], "cli-exports/technical_report.csv")
        self.assertEqual(payload["outputs"]["html"], "cli-exports/technical_report-html/index.html")
        self.assertNotIn(str(run_dir), command.stdout)
        self.assertTrue((output_dir / "technical_report.md").exists())
        self.assertTrue((output_dir / "technical_report.json").exists())
        self.assertTrue((output_dir / "technical_report.csv").exists())
        self.assertTrue((output_dir / "technical_report-html" / "index.html").exists())

        json_only_dir = run_dir / "cli-json-export"
        json_command = subprocess.run(
            [
                str(RUNNER),
                "export-report",
                "--run",
                str(run_dir),
                "--template",
                "technical_report",
                "--format",
                "json",
                "--output-dir",
                str(json_only_dir),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(json_command.returncode, 0, json_command.stderr)
        json_payload = json.loads(json_command.stdout)
        self.assertEqual(json_payload["output_dir"], "cli-json-export")
        self.assertEqual(set(json_payload["outputs"]), {"json"})
        self.assertEqual(json_payload["outputs"]["json"], "cli-json-export/technical_report.json")
        self.assertNotIn(str(run_dir), json_command.stdout)
        self.assertTrue((json_only_dir / "technical_report.json").exists())
        self.assertFalse((json_only_dir / "technical_report.md").exists())

    def test_export_rejects_non_completed_report_status(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["routing"] = [
            {
                "angle_id": "angle_visual",
                "question": "Visual evidence required",
                "modality": "visual_required",
                "reason": "image evidence is necessary",
                "search_queries": ["visual evidence"],
                "visual_tasks": ["screenshot_compare"],
                "max_images": 2,
            }
        ]
        evidence["images"] = [self.image()]
        evidence["claims"] = [
            self.claim(
                claim_id="claim_text_only",
                text="A visual claim referenced available image evidence but did not link usable visual support.",
                claim_type="mixed",
                supporting_images=["img_001"],
                verification_status="insufficient_evidence",
                review_status="needs_more_evidence",
                promotion_status="not_eligible",
                confidence="low",
                include_in_final_report=False,
            )
        ]
        evidence["claims"][0]["visual_supports"] = [self.visual_support()]
        self.write_json(run_dir / "evidence.json", evidence)

        status = synthesize_report(run=run_dir)

        self.assertEqual(status["status"], "failed_visual_evidence_unused")
        with self.assertRaisesRegex(ReportExportError, "failed_visual_evidence_unused"):
            export_report(run=run_dir, formats=["json"], output_dir=run_dir / "exports")

    def test_visual_required_report_fails_when_usable_image_evidence_is_unused(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["routing"] = [
            {
                "angle_id": "angle_visual",
                "question": "Visual evidence required",
                "modality": "visual_required",
                "reason": "image evidence is necessary",
                "search_queries": ["visual evidence"],
                "visual_tasks": ["screenshot_compare"],
                "max_images": 2,
            }
        ]
        evidence["images"] = [self.image()]
        evidence["claims"] = [
            self.claim(
                claim_id="claim_text_only",
                text="A visual claim referenced available image evidence but did not link usable visual support.",
                claim_type="mixed",
                supporting_images=["img_001"],
                verification_status="insufficient_evidence",
                review_status="needs_more_evidence",
                promotion_status="not_eligible",
                confidence="low",
                include_in_final_report=False,
            )
        ]
        evidence["claims"][0]["visual_supports"] = [self.visual_support()]
        self.write_json(run_dir / "evidence.json", evidence)

        status = synthesize_report(run=run_dir)

        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertEqual(status["status"], "failed_visual_evidence_unused")
        self.assertTrue(status["visual_evidence_unused"])
        self.assertEqual(status["usable_images"], ["img_001"])
        self.assertEqual(status["used_images"], [])
        self.assertIn("visual-required report is not passing", report)


if __name__ == "__main__":
    unittest.main()
