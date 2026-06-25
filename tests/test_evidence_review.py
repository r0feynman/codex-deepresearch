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

from deepresearch import (
    EvidenceReviewError,
    enforce_guardrails,
    inspect_evidence,
    list_reusable_claims,
    review_claim,
    synthesize_report,
    validate_artifacts,
    verify_claims,
)


class EvidenceReviewTests(unittest.TestCase):
    def temp_run(self, route: str = "visual_required") -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        run_dir = Path(temp_dir.name) / "review-run"
        run_dir.mkdir()
        self.write_json(run_dir / "evidence.json", self.base_evidence(route=route))
        return run_dir

    def write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def write_jsonl(self, path: Path, records: list[dict]) -> None:
        path.write_text(
            "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
            encoding="utf-8",
        )

    def load_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def base_evidence(self, route: str = "visual_required") -> dict:
        return {
            "schema_version": "0.1.0",
            "run_id": "review-run",
            "created_at": "2026-06-22T00:00:00Z",
            "question": "Evidence review workflow test",
            "mode": "codex-plugin",
            "search_provider": "codex-native",
            "vlm_provider": "codex-interactive",
            "routing": [
                {
                    "id": "angle_001",
                    "angle": "review workflow",
                    "modality": route,
                    "reason": "test route",
                    "visual_tasks": ["image_claim_alignment"] if route != "text_only" else [],
                    "max_images": 1 if route != "text_only" else 0,
                }
            ],
            "search_tasks": [
                {
                    "id": "task_search_001",
                    "angle_id": "angle_001",
                    "query": "Evidence review workflow test",
                    "freshness_requirement": "any",
                    "route": route,
                    "max_results": 5,
                    "source_policy": "allowed",
                }
            ],
            "sources": [self.source(route=route)],
            "images": [],
            "claims": [],
        }

    def source(self, *, source_id: str = "src_001", route: str = "visual_required") -> dict:
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
            "angle_id": "angle_001",
            "route": route,
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
            "policy_decision": "allowed",
            "policy_flags": [],
            "caveats": [],
            "candidate_id": "candidate_001",
            "fetch_id": "fetch_001",
            "provider_provenance": {"provider": "local-screenshot-fixture"},
        }

    def claim(
        self,
        *,
        claim_id: str,
        claim_type: str = "text",
        supporting_images: list[str] | None = None,
        visual_supports: list[dict] | None = None,
        route: str = "visual_required",
    ) -> dict:
        claim = {
            "id": claim_id,
            "text": "The source says Example.",
            "claim_type": claim_type,
            "supporting_sources": ["src_001"],
            "supporting_images": supporting_images if supporting_images is not None else [],
            "quote_spans": [
                {
                    "source_id": "src_001",
                    "quote": "Example",
                    "location": "paragraph 1",
                }
            ],
            "votes": [],
            "verification_status": "unverified",
            "review_status": "not_reviewed",
            "promotion_status": "not_eligible",
            "confidence": "low",
            "caveats": [],
            "angle_id": "angle_001",
            "route": route,
        }
        if visual_supports is not None:
            claim["visual_supports"] = visual_supports
        return claim

    def visual_support(self, image_id: str = "img_001") -> dict:
        return {
            "image_id": image_id,
            "observation_ref": f"images.{image_id}.observations[0]",
            "observation_index": 0,
            "observation_text": "Visible Example text is present.",
            "relation_type": "screenshot_support",
            "provider": "codex-interactive",
            "rationale": "Linked because claim and image cite the same source.",
            "confidence": 0.74,
        }

    def visual_observation(self, image_id: str = "img_001", claim_id: str = "claim_mixed") -> dict:
        return {
            "observation_id": "obs_001",
            "evidence_image_id": image_id,
            "task_id": "task_visual_001",
            "angle_id": "angle_001",
            "candidate_id": "candidate_001",
            "fetch_id": "fetch_001",
            "provider": "codex-interactive",
            "model_or_tool": "codex-interactive",
            "provider_mode": "fixture",
            "provider_provenance": {"provider": "local-screenshot-fixture"},
            "observation_status": "analyzed",
            "observations": ["Visible Example text is present."],
            "inferences": ["The image supports the visual claim."],
            "confidence": "medium",
            "policy_decision": "allowed",
            "policy_flags": [],
            "caveats": [],
            "verifier_links": [{"claim_id": claim_id}],
            "report_links": [],
            "estimated_cost_usd": 0,
            "actual_cost_usd": 0,
            "created_at": "2026-06-22T00:00:00Z",
        }

    def assert_valid_evidence(self, run_dir: Path) -> dict:
        result = validate_artifacts(evidence_path=run_dir / "evidence.json")
        self.assertTrue(result.valid, [error.to_dict() for error in result.errors])
        return self.load_json(run_dir / "evidence.json")

    def test_browser_shows_text_and_mixed_visual_provenance_chain(self) -> None:
        run_dir = self.temp_run(route="visual_required")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["images"] = [self.image()]
        evidence["claims"] = [
            self.claim(claim_id="claim_text", route="text_only"),
            self.claim(
                claim_id="claim_mixed",
                claim_type="mixed",
                supporting_images=["img_001"],
                visual_supports=[self.visual_support()],
            ),
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        self.write_jsonl(run_dir / "visual_observations.jsonl", [self.visual_observation()])
        verify_claims(run=run_dir)
        synthesize_report(run=run_dir)

        result = inspect_evidence(run=run_dir)

        by_id = {claim["claim_id"]: claim for claim in result["claims"]}
        text_chain = [item["kind"] for item in by_id["claim_text"]["provenance_chain"]]
        mixed_chain = [item["kind"] for item in by_id["claim_mixed"]["provenance_chain"]]
        self.assertIn("source", text_chain)
        self.assertIn("verifier_vote", text_chain)
        self.assertIn("report_citation", text_chain)
        self.assertIn("image", mixed_chain)
        self.assertIn("visual_observation", mixed_chain)
        self.assertIn("verifier_vote", mixed_chain)
        self.assertIn("report_citation", mixed_chain)
        self.assertEqual(by_id["claim_mixed"]["images"][0]["candidate_id"], "candidate_001")
        self.assertEqual(by_id["claim_mixed"]["visual_observations"][0]["observation_id"], "obs_001")

    def test_rejected_claim_is_excluded_until_supporting_evidence_changes(self) -> None:
        run_dir = self.temp_run(route="text_only")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [self.claim(claim_id="claim_text", route="text_only")]
        self.write_json(run_dir / "evidence.json", evidence)
        verify_claims(run=run_dir)

        review = review_claim(run=run_dir, claim_id="claim_text", decision="rejected")
        self.assertEqual(review["latest_event"]["review_status"], "human_rejected")
        status = synthesize_report(run=run_dir)
        self.assertEqual(status["claims_included"], 0)
        self.assertEqual(
            status["excluded_claims"][0]["exclusion_reasons"],
            ["review_not_confirmed", "human_rejected", "promotion_rejected"],
        )

        verify_claims(run=run_dir)
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(evidence["claims"][0]["review_status"], "human_rejected")

        evidence["claims"][0]["quote_spans"][0]["quote"] = "Updated Example"
        self.write_json(run_dir / "evidence.json", evidence)
        verify_claims(run=run_dir)
        status = synthesize_report(run=run_dir)

        evidence = self.assert_valid_evidence(run_dir)
        self.assertTrue(evidence["claims"][0]["review_stale"])
        self.assertEqual(evidence["claims"][0]["review_status"], "auto_reviewed")
        self.assertEqual(status["claims_included"], 1)

    def test_human_accepted_claim_is_reusable_when_rules_allow(self) -> None:
        run_dir = self.temp_run(route="text_only")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [self.claim(claim_id="claim_text", route="text_only")]
        self.write_json(run_dir / "evidence.json", evidence)
        verify_claims(run=run_dir)

        review_claim(
            run=run_dir,
            claim_id="claim_text",
            decision="accepted",
            reviewer="qa-reviewer",
            rationale="Source quote and verifier votes support the claim.",
        )
        reuse = list_reusable_claims(run=run_dir)
        evidence = self.assert_valid_evidence(run_dir)

        self.assertEqual(evidence["claims"][0]["review_status"], "human_accepted")
        self.assertEqual(evidence["claims"][0]["promotion_status"], "eligible")
        self.assertEqual(reuse["eligible_count"], 1)
        self.assertEqual(reuse["eligible_claims"][0]["claim_id"], "claim_text")
        self.assertTrue((run_dir / "review_status.json").exists())
        review_status = self.load_json(run_dir / "review_status.json")
        self.assertEqual(review_status["claims"][0]["reuse_eligible"], True)

    def test_guardrail_blocked_claim_cannot_be_accepted_or_promoted(self) -> None:
        run_dir = self.temp_run(route="text_only")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["sources"][0]["robots_policy"] = "disallowed"
        evidence["claims"] = [
            {
                **self.claim(claim_id="claim_blocked", route="text_only"),
                "verification_status": "supported",
                "review_status": "auto_reviewed",
                "promotion_status": "eligible",
                "include_in_final_report": True,
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        enforce_guardrails(run=run_dir)

        with self.assertRaises(EvidenceReviewError):
            review_claim(run=run_dir, claim_id="claim_blocked", decision="accepted")

        evidence = self.assert_valid_evidence(run_dir)
        claim = evidence["claims"][0]
        self.assertNotEqual(claim["review_status"], "human_accepted")
        self.assertEqual(claim["promotion_status"], "not_eligible")
        self.assertFalse(claim["include_in_final_report"])
        reuse = list_reusable_claims(run=run_dir)
        self.assertEqual(reuse["eligible_count"], 0)
        self.assertIn("claim_policy_blocked", reuse["excluded_claims"][0]["reuse_blockers"])

    def test_unsupported_claim_cannot_be_marked_accepted(self) -> None:
        run_dir = self.temp_run(route="text_only")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [
            {
                **self.claim(claim_id="claim_unsupported", route="text_only"),
                "verification_status": "insufficient_evidence",
                "review_status": "needs_more_evidence",
                "promotion_status": "not_eligible",
                "include_in_final_report": False,
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        with self.assertRaisesRegex(EvidenceReviewError, "verification_status:insufficient_evidence"):
            review_claim(run=run_dir, claim_id="claim_unsupported", decision="accepted")

        evidence = self.assert_valid_evidence(run_dir)
        self.assertEqual(evidence["claims"][0]["review_status"], "needs_more_evidence")
        self.assertEqual(evidence["claims"][0]["promotion_status"], "not_eligible")
        self.assertFalse((run_dir / "review_status.json").exists())

    def test_missing_claim_id_fails_without_mutating_artifacts(self) -> None:
        run_dir = self.temp_run(route="text_only")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [self.claim(claim_id="claim_text", route="text_only")]
        self.write_json(run_dir / "evidence.json", evidence)
        before = (run_dir / "evidence.json").read_text(encoding="utf-8")

        with self.assertRaisesRegex(EvidenceReviewError, "claim not found"):
            review_claim(run=run_dir, claim_id="missing_claim", decision="rejected")

        self.assertEqual((run_dir / "evidence.json").read_text(encoding="utf-8"), before)
        self.assertFalse((run_dir / "review_status.json").exists())

    def test_cli_review_and_browse_write_visible_review_artifacts(self) -> None:
        run_dir = self.temp_run(route="text_only")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [self.claim(claim_id="claim_text", route="text_only")]
        self.write_json(run_dir / "evidence.json", evidence)
        verify_claims(run=run_dir)

        review_command = subprocess.run(
            [
                str(RUNNER),
                "review-claim",
                "--run",
                str(run_dir),
                "--claim-id",
                "claim_text",
                "--decision",
                "needs_more_evidence",
                "--reviewer",
                "cli-reviewer",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(review_command.returncode, 0, review_command.stderr)
        review_payload = json.loads(review_command.stdout)
        self.assertEqual(review_payload["latest_event"]["review_status"], "needs_more_evidence")

        browse_command = subprocess.run(
            [str(RUNNER), "browse-evidence", "--run", str(run_dir), "--claim-id", "claim_text"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(browse_command.returncode, 0, browse_command.stderr)
        browse_payload = json.loads(browse_command.stdout)
        self.assertEqual(browse_payload["claims_returned"], 1)
        self.assertIn("review_status", browse_payload["artifacts"])
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(evidence["claims"][0]["human_review"]["reviewer"], "cli-reviewer")


if __name__ == "__main__":
    unittest.main()
