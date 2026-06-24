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

from deepresearch import enforce_guardrails, synthesize_report, validate_artifacts


class GuardrailsTests(unittest.TestCase):
    def temp_run(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        run_dir = Path(temp_dir.name) / "guardrail-run"
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
            "run_id": "guardrail-run",
            "created_at": "2026-06-22T00:00:00Z",
            "question": "Guardrail test",
            "mode": "codex-plugin",
            "search_provider": "codex-native",
            "vlm_provider": "codex-interactive",
            "routing": [],
            "search_tasks": [],
            "sources": [self.source()],
            "images": [],
            "claims": [],
        }

    def source(self, *, source_id: str = "src_001", quality: str = "primary") -> dict:
        return {
            "id": source_id,
            "type": "web",
            "url": "https://example.com/source",
            "title": "Example Source",
            "published_at": None,
            "accessed_at": "2026-06-22T00:00:00Z",
            "quality": quality,
            "retrieval_status": "fetched",
            "local_artifact_path": f"sources/{source_id}.html",
            "license_policy": "allowed",
            "robots_policy": "allowed",
            "policy_decision": "allowed",
            "policy_flags": [],
        }

    def image(
        self,
        *,
        image_id: str = "img_001",
        source_id: str = "src_001",
        origin: str = "screenshot",
    ) -> dict:
        return {
            "id": image_id,
            "source_id": source_id,
            "origin": origin,
            "image_url": "https://example.com/image.png",
            "page_url": "https://example.com/source",
            "local_artifact_path": f"images/{image_id}.png",
            "mime_type": "image/png",
            "width": 640,
            "height": 480,
            "hash": "sha256:test",
            "phash": None,
            "observations": ["Visible text is present."],
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
        claim_id: str = "claim_001",
        text: str = "The source says Example.",
        claim_type: str = "text",
        supporting_sources: list[str] | None = None,
        supporting_images: list[str] | None = None,
        confidence: str = "high",
        review_status: str = "human_accepted",
        promotion_status: str = "promoted_memory",
    ) -> dict:
        claim = {
            "id": claim_id,
            "text": text,
            "claim_type": claim_type,
            "supporting_sources": supporting_sources if supporting_sources is not None else ["src_001"],
            "supporting_images": supporting_images if supporting_images is not None else [],
            "quote_spans": [
                {
                    "source_id": "src_001",
                    "quote": "Example",
                    "location": "paragraph 1",
                }
            ]
            if claim_type in {"text", "mixed"}
            else [],
            "votes": [],
            "verification_status": "supported",
            "review_status": review_status,
            "promotion_status": promotion_status,
            "confidence": confidence,
            "caveats": [],
            "include_in_final_report": True,
        }
        if claim_type in {"visual", "mixed"} and supporting_images:
            image_id = supporting_images[0]
            claim["visual_supports"] = [
                {
                    "image_id": image_id,
                    "observation_ref": f"images.{image_id}.observations[0]",
                    "observation_index": 0,
                    "observation_text": "Visible text is present.",
                    "relation_type": "screenshot_support",
                    "provider": "codex-interactive",
                    "rationale": "Linked because guardrail fixture claim and image cite the same source.",
                    "confidence": 0.74,
                }
            ]
        return claim

    def assert_valid_evidence(self, run_dir: Path) -> dict:
        result = validate_artifacts(evidence_path=run_dir / "evidence.json")
        self.assertTrue(result.valid, [error.to_dict() for error in result.errors])
        return self.load_json(run_dir / "evidence.json")

    def test_login_and_captcha_sources_are_blocked_without_bypass(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        source = evidence["sources"][0]
        source["login_required"] = True
        source["raw_provider_metadata"] = {"captcha_required": True}
        evidence["claims"] = [self.claim(claim_id="claim_login")]
        self.write_json(run_dir / "evidence.json", evidence)

        status = enforce_guardrails(run=run_dir)

        self.assertEqual(status["status"], "completed")
        evidence = self.assert_valid_evidence(run_dir)
        source = evidence["sources"][0]
        claim = evidence["claims"][0]
        self.assertEqual(source["retrieval_status"], "failed")
        self.assertEqual(source["policy_decision"], "blocked")
        self.assertIn("login_gated", source["policy_flags"])
        self.assertIn("captcha_protected", source["policy_flags"])
        self.assertEqual(claim["verification_status"], "policy_blocked")
        self.assertNotEqual(claim["review_status"], "human_accepted")
        self.assertEqual(claim["promotion_status"], "not_eligible")

    def test_source_policy_flags_are_preserved_and_augmented(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        source = evidence["sources"][0]
        source["robots_policy"] = "disallowed"
        source["license_policy"] = "restricted"
        source["policy_flags"] = ["robots_disallowed", "paywall"]
        source["raw_provider_metadata"] = {"paywall": True}
        self.write_json(run_dir / "evidence.json", evidence)

        enforce_guardrails(run=run_dir)
        enforce_guardrails(run=run_dir)

        evidence = self.assert_valid_evidence(run_dir)
        flags = evidence["sources"][0]["policy_flags"]
        self.assertEqual(flags.count("robots_disallowed"), 1)
        self.assertIn("paywall", flags)
        self.assertIn("copyright_restricted", flags)

    def test_private_user_uploaded_image_gets_sensitive_possible(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["images"] = [self.image(origin="user_upload")]
        self.write_json(run_dir / "evidence.json", evidence)

        enforce_guardrails(run=run_dir)

        evidence = self.assert_valid_evidence(run_dir)
        image = evidence["images"][0]
        self.assertIn("sensitive_possible", image["policy_flags"])
        self.assertEqual(image["analysis_status"], "needs_manual_review")

    def test_high_risk_claim_without_primary_source_is_downgraded(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["sources"] = [self.source(quality="secondary")]
        evidence["claims"] = [
            self.claim(
                claim_id="claim_medical",
                text="The treatment dosage is safe for patients.",
                promotion_status="eligible",
                review_status="auto_reviewed",
            )
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        enforce_guardrails(run=run_dir)

        evidence = self.assert_valid_evidence(run_dir)
        claim = evidence["claims"][0]
        self.assertNotEqual(claim["confidence"], "high")
        self.assertEqual(claim["promotion_status"], "not_eligible")
        self.assertIn("high_risk_domain", claim["policy_flags"])
        self.assertIn("no_primary_source", claim["policy_flags"])

    def test_policy_blocked_evidence_cannot_remain_accepted_or_promoted(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["sources"][0]["robots_policy"] = "disallowed"
        evidence["claims"] = [self.claim(claim_id="claim_blocked")]
        self.write_json(run_dir / "evidence.json", evidence)

        enforce_guardrails(run=run_dir)

        evidence = self.assert_valid_evidence(run_dir)
        claim = evidence["claims"][0]
        self.assertEqual(claim["verification_status"], "policy_blocked")
        self.assertNotEqual(claim["review_status"], "human_accepted")
        self.assertNotIn("promoted", claim["promotion_status"])
        self.assertFalse(claim["include_in_final_report"])

    def test_report_truncates_copyrighted_claims_and_quotes(self) -> None:
        run_dir = self.temp_run()
        long_passage = "Copyrighted passage " + ("with many copied words " * 20)
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["sources"][0]["license_policy"] = "restricted"
        evidence["claims"] = [
            self.claim(
                claim_id="claim_copyright",
                text=long_passage,
                promotion_status="eligible",
                review_status="human_accepted",
            )
        ]
        evidence["claims"][0]["quote_spans"][0]["quote"] = long_passage
        self.write_json(run_dir / "evidence.json", evidence)

        synthesize_report(run=run_dir)

        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertNotIn(long_passage, report)
        self.assertIn("[copyright-truncated]", report)

    def test_unknown_license_image_cannot_be_eligible_without_human_acceptance(self) -> None:
        run_dir = self.temp_run()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["sources"][0]["license_policy"] = "unknown"
        evidence["images"] = [self.image()]
        evidence["claims"] = [
            self.claim(
                claim_id="claim_unknown_image",
                claim_type="visual",
                supporting_images=["img_001"],
                confidence="medium",
                review_status="auto_reviewed",
                promotion_status="eligible",
            )
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        enforce_guardrails(run=run_dir)

        evidence = self.assert_valid_evidence(run_dir)
        self.assertIn("unknown_license_image", evidence["images"][0]["policy_flags"])
        self.assertEqual(evidence["claims"][0]["promotion_status"], "not_eligible")

    def test_cli_enforce_guardrails_writes_status(self) -> None:
        run_dir = self.temp_run()
        command = subprocess.run(
            [str(RUNNER), "enforce-guardrails", "--run", str(run_dir)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(command.returncode, 0, command.stderr)
        payload = json.loads(command.stdout)
        self.assertEqual(payload["status"], "completed")
        self.assertTrue((run_dir / "guardrails_status.json").exists())


if __name__ == "__main__":
    unittest.main()
