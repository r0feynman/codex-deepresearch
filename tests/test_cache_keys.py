from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch.cache_keys import claim_cache_key, image_cache_key, source_cache_key


class CacheKeyTests(unittest.TestCase):
    def test_source_key_reuses_normalized_url_and_policy_context(self) -> None:
        source = {
            "type": "web",
            "url": "HTTPS://Example.com:443/path?b=2&a=1#fragment",
            "license_policy": "allowed",
            "robots_policy": "allowed",
            "policy_decision": "allowed",
            "policy_flags": ["robots_allowed"],
        }
        same = {
            **source,
            "url": "https://example.com/path?a=1&b=2",
            "policy_flags": ["robots_allowed"],
        }
        changed_policy = {**same, "robots_policy": "disallowed"}

        self.assertEqual(source_cache_key(source), source_cache_key(same))
        self.assertNotEqual(source_cache_key(source), source_cache_key(changed_policy))

    def test_image_key_uses_mime_size_hash_and_source_url_metadata(self) -> None:
        source = {"id": "src_001", "url": "https://example.com/source"}
        image = {
            "mime_type": "Image/PNG",
            "artifact_size_bytes": 128,
            "hash": "sha256:abc",
            "page_url": "https://example.com/source#ignored",
            "image_url": "https://cdn.example.com/image.png",
        }

        baseline = image_cache_key(image, source=source)
        self.assertEqual(
            baseline,
            image_cache_key({**image, "mime_type": "image/png; charset=binary"}, source=source),
        )
        self.assertNotEqual(baseline, image_cache_key({**image, "artifact_size_bytes": 129}, source=source))
        self.assertNotEqual(baseline, image_cache_key({**image, "hash": "sha256:def"}, source=source))
        self.assertNotEqual(
            baseline,
            image_cache_key(image, source={**source, "url": "https://example.com/changed"}),
        )

    def test_claim_key_uses_normalized_text_and_supporting_refs(self) -> None:
        sources = {
            "src_a": {
                "id": "src_a",
                "type": "web",
                "url": "https://example.com/a",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
            }
        }
        images = {
            "img_a": {
                "id": "img_a",
                "mime_type": "image/png",
                "artifact_size_bytes": 10,
                "hash": "sha256:abc",
                "page_url": "https://example.com/a",
            }
        }
        claim = {
            "text": "The   Claim Uses   Normalized Text.",
            "claim_type": "mixed",
            "supporting_sources": ["src_a"],
            "supporting_images": ["img_a"],
            "quote_spans": [{"source_id": "src_a", "quote": "The claim uses normalized text."}],
        }
        same = {**claim, "text": "the claim uses normalized text."}
        changed_ref = {**claim, "supporting_images": []}

        self.assertEqual(
            claim_cache_key(claim, sources_by_id=sources, images_by_id=images),
            claim_cache_key(same, sources_by_id=sources, images_by_id=images),
        )
        self.assertNotEqual(
            claim_cache_key(claim, sources_by_id=sources, images_by_id=images),
            claim_cache_key(changed_ref, sources_by_id=sources, images_by_id=images),
        )


if __name__ == "__main__":
    unittest.main()
