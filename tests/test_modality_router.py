from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch import route_angle


class ModalityRouterTests(unittest.TestCase):
    def test_api_doc_question_routes_text_only(self) -> None:
        decision = route_angle(
            question="What changed in the OpenAI image generation API docs?",
            angle="official image API documentation and release notes",
            max_images=12,
        )

        self.assertEqual(decision.modality, "text_only")
        self.assertEqual(decision.visual_tasks, [])
        self.assertEqual(decision.max_images, 0)

    def test_ui_comparison_routes_visual_required(self) -> None:
        decision = route_angle(
            question="Compare the checkout UI for these two products.",
            angle="screenshot and layout comparison",
            max_images=12,
        )

        self.assertEqual(decision.modality, "visual_required")
        self.assertIn("image_claim_alignment", decision.visual_tasks)
        self.assertIn("layout_review", decision.visual_tasks)
        self.assertEqual(decision.max_images, 12)

    def test_market_report_routes_visual_optional(self) -> None:
        decision = route_angle(
            question="Create a market report for AI code review tools.",
            angle="market share, pricing, benchmark posts, and competitor trends",
            max_images=12,
        )

        self.assertEqual(decision.modality, "visual_optional")
        self.assertEqual(decision.visual_tasks, ["image_claim_alignment"])
        self.assertEqual(decision.max_images, 4)


if __name__ == "__main__":
    unittest.main()
