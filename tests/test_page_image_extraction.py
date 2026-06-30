from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import Request


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
RUNNER = ROOT / "plugins" / "codex-deepresearch" / "scripts" / "codex-deepresearch"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch import (  # noqa: E402
    FetchResponse,
    extract_and_fetch_page_images,
    extract_page_image_candidates,
    prepare_run,
    validate_artifacts,
)
from deepresearch.visual_artifacts import (  # noqa: E402
    IMAGE_FETCH_STATUS_FILENAME,
    VISUAL_CANDIDATES_FILENAME,
    VISUAL_PROVIDER_STATUS_FILENAME,
    VISUAL_SEARCH_PLAN_FILENAME,
    validate_visual_artifacts,
)
import deepresearch.page_image_extraction as page_images  # noqa: E402


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03\x02\x00\xef\xbf\xa7\xdb"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


class PageImageExtractionTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def read_jsonl(self, path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def prepared_run_with_pages(self) -> tuple[Path, dict[str, bytes]]:
        prepared = prepare_run(
            question="Extract page images for a product comparison",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        (run_dir / "sources").mkdir(exist_ok=True)
        local_image = run_dir / "sources" / "local-chart.png"
        local_image.write_bytes(PNG_1X1 + b"local")
        local_uri = local_image.resolve().as_uri()
        local_page = run_dir / "sources" / "local-page.html"
        local_page.write_text(
            f"""
            <html>
              <body>
                <p>Local fixture context before chart.</p>
                <figure>
                  <img src="{local_uri}" alt="Local chart" width="1" height="1">
                  <figcaption>Caption for the local chart.</figcaption>
                </figure>
              </body>
            </html>
            """,
            encoding="utf-8",
        )
        allowed_page = run_dir / "sources" / "allowed-page.html"
        allowed_page.write_text(
            f"""
            <html>
              <head>
                <meta property="og:image:alt" content="Hero product alt">
                <meta property="og:image" content="https://img.example.test/hero.png">
              </head>
              <body>
                <p>Intro context before product media.</p>
                <img src="https://img.example.test/hero.png" alt="Duplicate hero">
                <img src="{local_uri}" alt="Forbidden local from remote">
                <picture>
                  <source srcset="https://img.example.test/vector.svg 1x">
                </picture>
                <img data-src="https://img.example.test/too-large.png" alt="Large lazy">
                <img src="https://img.example.test/fail.png" alt="Failed fetch">
                <img src="https://img.example.test/content-duplicate.png"
                     alt="Content duplicate">
                <img src="https://img.example.test/phash-a.png"
                     alt="First phash" data-phash="same-object">
                <img src="https://img.example.test/phash-b.png"
                     alt="Second phash" data-phash="same-object">
                <img src="https://img.example.test/fourth.png" alt="Fourth selected">
                <img src="https://img.example.test/budget.png" alt="Budget candidate">
              </body>
            </html>
            """,
            encoding="utf-8",
        )
        blocked_page = run_dir / "sources" / "blocked-page.html"
        blocked_page.write_text(
            (
                '<html><body><img src="https://img.example.test/blocked.png" '
                'alt="Blocked"></body></html>'
            ),
            encoding="utf-8",
        )
        manual_page = run_dir / "sources" / "manual-page.html"
        manual_page.write_text(
            (
                '<html><body><img src="https://img.example.test/manual.png" '
                'alt="Manual"></body></html>'
            ),
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_local_page",
                "type": "web",
                "url": local_page.resolve().as_uri(),
                "title": "Local visual fixture page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/local-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "search_result_id": "search_local",
                "task_id": "task_search_001",
                "angle_id": "angle_001",
                "route": "visual_required",
            },
            {
                "id": "src_allowed_page",
                "type": "web",
                "url": "https://example.test/product/",
                "title": "Allowed visual page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/allowed-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "search_result_id": "search_allowed",
                "task_id": "task_search_001",
                "angle_id": "angle_001",
                "route": "visual_required",
            },
            {
                "id": "src_blocked_page",
                "type": "web",
                "url": "https://example.test/blocked/",
                "title": "Blocked visual page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "secondary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/blocked-page.html",
                "license_policy": "allowed",
                "robots_policy": "disallowed",
                "policy_decision": "blocked",
                "policy_flags": ["robots_disallowed"],
                "search_result_id": "search_blocked",
                "task_id": "task_search_001",
                "angle_id": "angle_001",
                "route": "visual_required",
            },
            {
                "id": "src_manual_page",
                "type": "web",
                "url": "https://example.test/manual/",
                "title": "Manual review visual page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "secondary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/manual-page.html",
                "license_policy": "manual_review",
                "robots_policy": "allowed",
                "policy_decision": "manual_review",
                "policy_flags": ["copyright_manual_review"],
                "search_result_id": "search_manual",
                "task_id": "task_search_001",
                "angle_id": "angle_001",
                "route": "visual_required",
            },
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        image_bytes = {
            "https://img.example.test/hero.png": PNG_1X1 + b"hero",
            "https://img.example.test/vector.svg": b"<svg></svg>",
            "https://img.example.test/too-large.png": PNG_1X1 + b"x" * 200,
            "https://img.example.test/phash-a.png": PNG_1X1 + b"phash-a",
            "https://img.example.test/phash-b.png": PNG_1X1 + b"phash-b",
            "https://img.example.test/content-duplicate.png": PNG_1X1 + b"local",
            "https://img.example.test/fourth.png": PNG_1X1 + b"fourth",
            "https://img.example.test/budget.png": PNG_1X1 + b"budget",
            "https://img.example.test/blocked.png": PNG_1X1 + b"blocked",
            "https://img.example.test/manual.png": PNG_1X1 + b"manual",
        }
        return run_dir, image_bytes

    def test_html_extraction_captures_og_srcset_lazy_caption_alt_and_context(self) -> None:
        html = """
        <html><head><meta property="og:image" content="/hero.png"></head>
        <body>
          <p>Nearby context</p>
          <figure>
            <img src="/body.png" alt="Body alt" srcset="/body-2x.png 2x" data-src="/lazy.png">
            <figcaption>Figure caption</figcaption>
          </figure>
        </body></html>
        """

        candidates = extract_page_image_candidates(
            html=html,
            page_url="https://example.test/article/",
        )

        origins = {candidate["origin"] for candidate in candidates}
        self.assertTrue({"open_graph", "page_image", "srcset", "lazy_loaded"}.issubset(origins))
        body = next(candidate for candidate in candidates if candidate["origin"] == "page_image")
        self.assertEqual(body["alt_text"], "Body alt")
        self.assertEqual(body["caption_text"], "Figure caption")
        self.assertIn("Nearby context", body["surrounding_text"])
        self.assertEqual(body["image_url"], "https://example.test/body.png")

    def test_page_image_fetch_cache_writes_lineage_statuses_and_evidence_images(self) -> None:
        run_dir, image_bytes = self.prepared_run_with_pages()

        def transport(url: str) -> FetchResponse:
            if url.startswith("file:"):
                path = Path(unquote(urlparse(url).path))
                return FetchResponse(
                    content=path.read_bytes(),
                    mime_type="image/png",
                    status_code=None,
                    final_url=url,
                )
            if url == "https://img.example.test/fail.png":
                return FetchResponse(
                    content=None,
                    mime_type=None,
                    status_code=503,
                    final_url=url,
                    error_code="http_503",
                )
            content = image_bytes[url]
            mime_type = "image/svg+xml" if url.endswith(".svg") else "image/png"
            return FetchResponse(
                content=content,
                mime_type=mime_type,
                status_code=200,
                final_url=url,
            )

        result = extract_and_fetch_page_images(
            run=run_dir,
            transport=transport,
            max_image_bytes=140,
            max_fetches=4,
            provider_mode="fixture",
        )

        self.assertEqual(result["status"], "page_images_processed")
        self.assertTrue(
            result["evidence_validation"]["valid"],
            result["evidence_validation"]["errors"],
        )
        self.assertTrue(
            result["visual_artifact_validation"]["valid"],
            result["visual_artifact_validation"]["errors"],
        )
        for filename in (
            VISUAL_SEARCH_PLAN_FILENAME,
            VISUAL_CANDIDATES_FILENAME,
            IMAGE_FETCH_STATUS_FILENAME,
            VISUAL_PROVIDER_STATUS_FILENAME,
        ):
            self.assertTrue((run_dir / filename).is_file(), filename)

        candidates = self.read_jsonl(run_dir / VISUAL_CANDIDATES_FILENAME)
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        evidence = self.read_json(run_dir / "evidence.json")
        status_set = {record["fetch_status"] for record in fetches}
        self.assertTrue(
            {
                "fetched",
                "failed",
                "skipped",
                "policy_blocked",
                "budget_pruned",
                "unsupported_mime",
                "too_large",
                "deduped",
            }.issubset(status_set)
        )
        self.assertEqual(len(evidence["images"]), result["images_linked"])
        self.assertEqual(result["images_linked"], 4)

        by_image = {image["id"]: image for image in evidence["images"]}
        for fetch in fetches:
            if fetch["fetch_status"] != "fetched":
                continue
            image = by_image[fetch["evidence_image_id"]]
            self.assertEqual(image["candidate_id"], fetch["candidate_id"])
            self.assertEqual(image["fetch_id"], fetch["fetch_id"])
            self.assertEqual(image["local_artifact_path"], fetch["local_artifact_path"])
            self.assertEqual(image["hash"], fetch["hash"])
            self.assertEqual(image["policy_decision"], fetch["policy_decision"])
            self.assertEqual(image["provider_mode"], fetch["provider_mode"])
            self.assertEqual(image["license_policy"], "allowed")
            self.assertEqual(image["robots_policy"], "allowed")

        local_image = next(
            image for image in evidence["images"] if image["source_id"] == "src_local_page"
        )
        self.assertEqual(local_image["origin"], "page_image")
        self.assertEqual(local_image["candidate_origin"], "page_image")
        self.assertTrue(
            any("Caption for the local chart" in item for item in local_image["observations"])
        )
        og_image = next(
            image for image in evidence["images"] if image["candidate_origin"] == "open_graph"
        )
        self.assertEqual(og_image["origin"], "page_image")
        self.assertEqual(og_image["html_origin"], "open_graph")
        self.assertTrue(og_image["observations"])
        self.assertTrue(og_image["hash"].startswith("sha256:"))
        self.assertTrue(og_image["phash"].startswith("phash:"))

        deduped = [record for record in fetches if record["fetch_status"] == "deduped"]
        reasons = {record["failure_code"] for record in deduped}
        self.assertTrue(
            {"duplicate_image_url", "duplicate_content_hash", "duplicate_phash"}.issubset(reasons)
        )
        for record in deduped:
            self.assertTrue(record["dedupe_target_candidate_id"])
            self.assertTrue(record["dedupe_target_fetch_id"])

        candidate_by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
        og_candidate = next(
            candidate for candidate in candidates if candidate["candidate_origin"] == "open_graph"
        )
        self.assertEqual(og_candidate["origin"], "page_image")
        self.assertEqual(og_candidate["html_origin"], "open_graph")
        og_fetch = next(fetch for fetch in fetches if fetch["candidate_id"] == og_candidate["candidate_id"])
        self.assertEqual(og_fetch["origin"], "page_image")
        self.assertEqual(og_fetch["candidate_origin"], "open_graph")
        self.assertEqual(og_fetch["html_origin"], "open_graph")
        policy_blocks = {
            record["failure_code"]: candidate_by_id[record["candidate_id"]]
            for record in fetches
            if record["fetch_status"] == "policy_blocked"
        }
        self.assertIn("policy_blocked", policy_blocks)
        self.assertIn("local_url_from_remote_page", policy_blocks)
        blocked_candidate = policy_blocks["policy_blocked"]
        self.assertEqual(blocked_candidate["policy_decision"], "blocked")
        self.assertIn("robots_disallowed", blocked_candidate["policy_flags"])
        local_blocked_candidate = policy_blocks["local_url_from_remote_page"]
        self.assertEqual(local_blocked_candidate["policy_decision"], "blocked")
        self.assertIn("local_url_from_remote_page", local_blocked_candidate["policy_flags"])
        budget = next(record for record in fetches if record["fetch_status"] == "budget_pruned")
        self.assertEqual(
            candidate_by_id[budget["candidate_id"]]["candidate_status"],
            "budget_pruned",
        )

        evidence_validation = validate_artifacts(evidence_path=run_dir / "evidence.json")
        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertTrue(
            evidence_validation.valid,
            [error.to_dict() for error in evidence_validation.errors],
        )
        self.assertTrue(
            visual_validation.valid,
            [error.to_dict() for error in visual_validation.errors],
        )

    def test_real_mode_fetches_remote_source_html_when_local_artifact_missing_or_empty(
        self,
    ) -> None:
        prepared = prepare_run(
            question="Recover page images from reachable remote source HTML",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        (run_dir / "sources").mkdir(exist_ok=True)
        tiny_page = run_dir / "sources" / "tiny-page.html"
        tiny_page.write_text("<html><body>No images here.</body></html>", encoding="utf-8")
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_missing_remote_page",
                "type": "web",
                "url": "https://science.example.test/missing-source/",
                "title": "Missing local source",
                "published_at": None,
                "accessed_at": "2026-06-30T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/missing-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "search_result_id": "search_missing_remote_page",
                "angle_id": "angle_001",
                "route": "visual_required",
            },
            {
                "id": "src_tiny_remote_page",
                "type": "web",
                "url": "https://science.example.test/tiny-source/",
                "title": "Tiny local source",
                "published_at": None,
                "accessed_at": "2026-06-30T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/tiny-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "search_result_id": "search_tiny_remote_page",
                "angle_id": "angle_001",
                "route": "visual_required",
            },
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        remote_html = {
            "https://science.example.test/missing-source/": """
                <html><body>
                  <img src="/media/missing-a.png" alt="Missing remote A">
                  <img src="/media/missing-b.png" alt="Missing remote B">
                </body></html>
            """,
            "https://science.example.test/tiny-source/": """
                <html><head>
                  <meta property="og:image" content="/media/tiny-og.png">
                </head><body></body></html>
            """,
        }
        calls: list[str] = []

        def transport(url: str) -> FetchResponse:
            calls.append(url)
            if url in remote_html:
                return FetchResponse(
                    content=remote_html[url].encode("utf-8"),
                    mime_type="text/html",
                    status_code=200,
                    final_url=url,
                )
            return FetchResponse(
                content=PNG_1X1 + url.encode("utf-8"),
                mime_type="image/png",
                status_code=200,
                final_url=url,
            )

        result = extract_and_fetch_page_images(
            run=run_dir,
            transport=transport,
            provider_mode="real",
            max_fetches=3,
        )

        self.assertEqual(result["remote_page_fetches"], 2)
        self.assertEqual(result["remote_page_fetch_status_counts"], {"fetched": 2})
        self.assertEqual(result["candidate_records"], 3)
        self.assertEqual(result["images_linked"], 3)
        self.assertEqual(calls[:2], list(remote_html))
        candidates = self.read_jsonl(run_dir / VISUAL_CANDIDATES_FILENAME)
        self.assertEqual({candidate["provider"] for candidate in candidates}, {"page-image-extractor"})
        self.assertEqual(
            {candidate["provider_provenance"]["page_html_source"] for candidate in candidates},
            {"remote_fetch"},
        )
        self.assertTrue(
            {
                "https://science.example.test/media/missing-a.png",
                "https://science.example.test/media/missing-b.png",
                "https://science.example.test/media/tiny-og.png",
            }.issubset({candidate["image_url"] for candidate in candidates})
        )
        provider_status = self.read_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME)
        provider = provider_status["providers"][0]
        self.assertTrue(provider["external_network_call"])
        self.assertEqual(provider["diagnostics"]["remote_page_fetches"], 2)

    def test_fixture_mode_does_not_fetch_remote_source_html_for_missing_local_artifact(
        self,
    ) -> None:
        prepared = prepare_run(
            question="Fixture mode should remain no-network",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_missing_fixture_page",
                "type": "web",
                "url": "https://science.example.test/missing-source/",
                "title": "Missing local source",
                "published_at": None,
                "accessed_at": "2026-06-30T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/missing-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "visual_required",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        calls: list[str] = []

        def transport(url: str) -> FetchResponse:
            calls.append(url)
            raise AssertionError(f"unexpected network fixture call: {url}")

        result = extract_and_fetch_page_images(
            run=run_dir,
            transport=transport,
            provider_mode="fixture",
        )

        self.assertEqual(calls, [])
        self.assertEqual(result["page_sources_scanned"], 0)
        self.assertEqual(result["remote_page_fetches"], 0)
        self.assertEqual(result["candidate_records"], 0)
        self.assertEqual(result["images_linked"], 0)

    def test_remote_page_file_url_is_policy_blocked_without_local_read(self) -> None:
        prepared = prepare_run(
            question="Reject remote page local file image",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        secret_dir = self.temp_runs_dir()
        secret_image = secret_dir / "private-chart.png"
        secret_image.write_bytes(PNG_1X1 + b"private")
        (run_dir / "sources").mkdir(exist_ok=True)
        page = run_dir / "sources" / "remote-page.html"
        page.write_text(
            f'<html><body><img src="{secret_image.resolve().as_uri()}" alt="Private"></body></html>',
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_remote_page",
                "type": "web",
                "url": "https://example.test/remote/",
                "title": "Remote page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/remote-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "visual_required",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        result = extract_and_fetch_page_images(run=run_dir, provider_mode="real")

        self.assertEqual(result["images_linked"], 0)
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        self.assertEqual(len(fetches), 1)
        self.assertEqual(fetches[0]["fetch_status"], "policy_blocked")
        self.assertEqual(fetches[0]["failure_code"], "local_url_from_remote_page")
        self.assertFalse((run_dir / "images").exists())

    def test_local_fixture_page_file_url_outside_run_is_policy_blocked_without_local_read(
        self,
    ) -> None:
        prepared = prepare_run(
            question="Reject local page outside file image",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        secret_dir = self.temp_runs_dir()
        secret_image = secret_dir / "private-chart.png"
        secret_image.write_bytes(PNG_1X1 + b"private")
        (run_dir / "sources").mkdir(exist_ok=True)
        page = run_dir / "sources" / "local-page.html"
        page.write_text(
            f'<html><body><img src="{secret_image.resolve().as_uri()}" alt="Private"></body></html>',
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_local_page",
                "type": "fixture",
                "url": page.resolve().as_uri(),
                "title": "Local page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/local-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "visual_required",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        calls: list[str] = []

        def transport(url: str) -> FetchResponse:
            calls.append(url)
            return FetchResponse(content=secret_image.read_bytes(), mime_type="image/png")

        result = extract_and_fetch_page_images(run=run_dir, transport=transport)

        self.assertEqual(calls, [])
        self.assertEqual(result["images_linked"], 0)
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        self.assertEqual(len(fetches), 1)
        self.assertEqual(fetches[0]["fetch_status"], "policy_blocked")
        self.assertEqual(fetches[0]["failure_code"], "local_file_outside_run")
        self.assertFalse((run_dir / "images").exists())

    def test_hard_source_policy_flags_block_image_fetch_without_transport(self) -> None:
        prepared = prepare_run(
            question="Policy blocked source should not fetch page images",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        (run_dir / "sources").mkdir(exist_ok=True)
        page = run_dir / "sources" / "policy-page.html"
        page.write_text(
            '<html><body><img src="https://img.example.test/policy.png" alt="Policy"></body></html>',
            encoding="utf-8",
        )
        hard_flags = [
            "access_controlled",
            "captcha_protected",
            "login_gated",
            "paywall",
            "pii_detected",
        ]
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_policy_page",
                "type": "web",
                "url": "https://example.test/policy/",
                "title": "Policy page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/policy-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": hard_flags,
                "angle_id": "angle_001",
                "route": "visual_required",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        calls: list[str] = []

        def transport(url: str) -> FetchResponse:
            calls.append(url)
            return FetchResponse(content=PNG_1X1, mime_type="image/png", status_code=200)

        result = extract_and_fetch_page_images(run=run_dir, transport=transport)

        self.assertEqual(calls, [])
        self.assertEqual(result["images_linked"], 0)
        candidates = self.read_jsonl(run_dir / VISUAL_CANDIDATES_FILENAME)
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        self.assertEqual(candidates[0]["policy_decision"], "blocked")
        self.assertTrue(set(hard_flags).issubset(set(candidates[0]["policy_flags"])))
        self.assertEqual(fetches[0]["fetch_status"], "policy_blocked")
        self.assertEqual(fetches[0]["failure_code"], "policy_blocked")

    def test_manual_review_policy_flags_skip_image_fetch_without_transport(self) -> None:
        prepared = prepare_run(
            question="Manual review source should not fetch page images",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        (run_dir / "sources").mkdir(exist_ok=True)
        page = run_dir / "sources" / "manual-review-page.html"
        page.write_text(
            '<html><body><img src="https://img.example.test/manual-review.png" alt="Manual"></body></html>',
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_manual_review_page",
                "type": "web",
                "url": "https://example.test/manual-review/",
                "title": "Manual review page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/manual-review-page.html",
                "license_policy": "manual_review",
                "robots_policy": "manual_review",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "visual_required",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        calls: list[str] = []

        def transport(url: str) -> FetchResponse:
            calls.append(url)
            return FetchResponse(content=PNG_1X1, mime_type="image/png", status_code=200)

        result = extract_and_fetch_page_images(run=run_dir, transport=transport)

        self.assertEqual(calls, [])
        self.assertEqual(result["images_linked"], 0)
        candidates = self.read_jsonl(run_dir / VISUAL_CANDIDATES_FILENAME)
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        self.assertEqual(candidates[0]["policy_decision"], "manual_review")
        self.assertIn("copyright_manual_review", candidates[0]["policy_flags"])
        self.assertIn("robots_manual_review", candidates[0]["policy_flags"])
        self.assertEqual(fetches[0]["fetch_status"], "skipped")
        self.assertEqual(fetches[0]["failure_code"], "policy_manual_review")

    def test_remote_page_private_network_url_is_policy_blocked(self) -> None:
        prepared = prepare_run(
            question="Reject remote page private network image",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        (run_dir / "sources").mkdir(exist_ok=True)
        page = run_dir / "sources" / "remote-page.html"
        page.write_text(
            '<html><body><img src="http://127.0.0.1/private.png" alt="Private"></body></html>',
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_remote_page",
                "type": "web",
                "url": "https://example.test/remote/",
                "title": "Remote page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/remote-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "visual_required",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        calls: list[str] = []

        def transport(url: str) -> FetchResponse:
            calls.append(url)
            return FetchResponse(content=PNG_1X1, mime_type="image/png", status_code=200)

        result = extract_and_fetch_page_images(run=run_dir, transport=transport)

        self.assertEqual(calls, [])
        self.assertEqual(result["images_linked"], 0)
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        self.assertEqual(fetches[0]["fetch_status"], "policy_blocked")
        self.assertEqual(fetches[0]["failure_code"], "private_network_url_from_remote_page")

    def test_remote_page_obvious_internal_hosts_are_policy_blocked_without_transport(
        self,
    ) -> None:
        prepared = prepare_run(
            question="Reject remote page obvious internal image hosts",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        (run_dir / "sources").mkdir(exist_ok=True)
        urls = [
            "http://metadata.google.internal/latest.png",
            "http://host.docker.internal/private.png",
            "http://intranet/private.png",
        ]
        page = run_dir / "sources" / "remote-page.html"
        page.write_text(
            "<html><body>"
            + "".join(f'<img src="{url}" alt="Private">' for url in urls)
            + "</body></html>",
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_remote_page",
                "type": "web",
                "url": "https://example.test/remote/",
                "title": "Remote page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/remote-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "visual_required",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        calls: list[str] = []

        def transport(url: str) -> FetchResponse:
            calls.append(url)
            return FetchResponse(content=PNG_1X1, mime_type="image/png", status_code=200)

        result = extract_and_fetch_page_images(run=run_dir, transport=transport)

        self.assertEqual(calls, [])
        self.assertEqual(result["images_linked"], 0)
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        self.assertEqual(len(fetches), len(urls))
        self.assertEqual({record["fetch_status"] for record in fetches}, {"policy_blocked"})
        self.assertEqual(
            {record["failure_code"] for record in fetches},
            {"private_network_url_from_remote_page"},
        )

    def test_remote_page_dns_resolved_private_host_is_policy_blocked_without_fetch(
        self,
    ) -> None:
        prepared = prepare_run(
            question="Reject DNS-resolved private image host",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        (run_dir / "sources").mkdir(exist_ok=True)
        page = run_dir / "sources" / "remote-page.html"
        page.write_text(
            '<html><body><img src="http://public-name.example.test/private.png" alt="Private"></body></html>',
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_remote_page",
                "type": "web",
                "url": "https://example.test/remote/",
                "title": "Remote page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/remote-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "visual_required",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        original_getaddrinfo = page_images.socket.getaddrinfo

        def fake_getaddrinfo(host: str, port: int, *args: object, **kwargs: object) -> list[tuple]:
            self.assertEqual(host, "public-name.example.test")
            return [(page_images.socket.AF_INET, page_images.socket.SOCK_STREAM, 6, "", ("10.0.0.4", port))]

        page_images.socket.getaddrinfo = fake_getaddrinfo
        try:
            result = extract_and_fetch_page_images(run=run_dir)
        finally:
            page_images.socket.getaddrinfo = original_getaddrinfo

        self.assertEqual(result["images_linked"], 0)
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        self.assertEqual(fetches[0]["fetch_status"], "policy_blocked")
        self.assertEqual(fetches[0]["failure_code"], "private_network_url_from_remote_page")

    def test_remote_page_noncanonical_private_hosts_are_policy_blocked(self) -> None:
        prepared = prepare_run(
            question="Reject remote page noncanonical private network images",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        (run_dir / "sources").mkdir(exist_ok=True)
        urls = [
            "http://2130706433/private.png",
            "http://0177.0.0.1/private.png",
            "http://127.1/private.png",
            "http://0x7f.0.0.1/private.png",
        ]
        page = run_dir / "sources" / "remote-page.html"
        page.write_text(
            "<html><body>"
            + "".join(f'<img src="{url}" alt="Private">' for url in urls)
            + "</body></html>",
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_remote_page",
                "type": "web",
                "url": "https://example.test/remote/",
                "title": "Remote page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/remote-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "visual_required",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        calls: list[str] = []

        def transport(url: str) -> FetchResponse:
            calls.append(url)
            return FetchResponse(content=PNG_1X1, mime_type="image/png", status_code=200)

        result = extract_and_fetch_page_images(run=run_dir, transport=transport)

        self.assertEqual(calls, [])
        self.assertEqual(result["images_linked"], 0)
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        self.assertEqual(len(fetches), len(urls))
        self.assertEqual(
            {record["fetch_status"] for record in fetches},
            {"policy_blocked"},
        )
        self.assertEqual(
            {record["failure_code"] for record in fetches},
            {"private_network_url_from_remote_page"},
        )

    def test_malformed_image_url_is_skipped_without_transport(self) -> None:
        prepared = prepare_run(
            question="Malformed image URL should not crash extraction",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        (run_dir / "sources").mkdir(exist_ok=True)
        page = run_dir / "sources" / "malformed-page.html"
        page.write_text(
            '<html><body><img src="http://[::1" alt="Bad"></body></html>',
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_malformed_page",
                "type": "web",
                "url": "https://example.test/malformed/",
                "title": "Malformed page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/malformed-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "visual_required",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        calls: list[str] = []

        def transport(url: str) -> FetchResponse:
            calls.append(url)
            return FetchResponse(content=PNG_1X1, mime_type="image/png", status_code=200)

        result = extract_and_fetch_page_images(run=run_dir, transport=transport)

        self.assertEqual(calls, [])
        self.assertEqual(result["images_linked"], 0)
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        self.assertEqual(len(fetches), 1)
        self.assertEqual(fetches[0]["fetch_status"], "skipped")
        self.assertEqual(fetches[0]["failure_code"], "unsupported_url_scheme")

    def test_source_html_path_must_be_run_relative(self) -> None:
        prepared = prepare_run(
            question="External HTML artifact should not be read",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        external_page = self.temp_runs_dir() / "external.html"
        external_page.write_text(
            '<html><body><img src="https://img.example.test/external.png" alt="External"></body></html>',
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_external_page",
                "type": "web",
                "url": "https://example.test/external/",
                "title": "External page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": str(external_page),
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "visual_required",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        calls: list[str] = []

        def transport(url: str) -> FetchResponse:
            calls.append(url)
            return FetchResponse(content=PNG_1X1, mime_type="image/png", status_code=200)

        result = extract_and_fetch_page_images(run=run_dir, transport=transport)

        self.assertEqual(calls, [])
        self.assertEqual(result["page_sources_scanned"], 0)
        self.assertEqual(result["candidate_records"], 0)
        self.assertEqual(result["fetch_records"], 0)
        self.assertEqual(result["images_linked"], 0)

    def test_private_network_redirect_target_is_blocked_before_following(self) -> None:
        handler = page_images._PrivateNetworkBlockingRedirectHandler()
        request = Request("https://images.example.test/public.png")

        with self.assertRaises(ValueError):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "http://169.254.169.254/latest/meta-data/",
            )

    def test_max_fetches_caps_successful_fetches_and_tops_up_after_initial_failures(self) -> None:
        prepared = prepare_run(
            question="Successful fetches should be capped after top-up",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        (run_dir / "sources").mkdir(exist_ok=True)
        page = run_dir / "sources" / "attempts-page.html"
        page.write_text(
            "<html><body>"
            + "".join(
                f'<img src="https://img.example.test/candidate-{index}{suffix}" alt="{index}">'
                for index, suffix in (
                    (1, ".txt"),
                    (2, ".txt"),
                    (3, ".png"),
                    (4, ".png"),
                    (5, ".png"),
                    (6, ".png"),
                )
            )
            + "</body></html>",
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_attempts_page",
                "type": "web",
                "url": "https://example.test/attempts/",
                "title": "Attempts page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/attempts-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "visual_required",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        calls: list[str] = []

        def transport(url: str) -> FetchResponse:
            calls.append(url)
            if url.endswith(".txt"):
                return FetchResponse(
                    content=b"not an image",
                    mime_type="text/plain",
                    status_code=200,
                    final_url=url,
                )
            return FetchResponse(
                content=PNG_1X1 + url.encode("utf-8"),
                mime_type="image/png",
                status_code=200,
                final_url=url,
            )

        extract_and_fetch_page_images(run=run_dir, transport=transport, max_fetches=3)

        self.assertEqual(len(calls), 5)
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        statuses = [record["fetch_status"] for record in fetches]
        self.assertEqual(statuses.count("unsupported_mime"), 2)
        self.assertEqual(statuses.count("fetched"), 3)
        self.assertEqual(statuses.count("budget_pruned"), 1)

    def test_max_fetches_exhausts_eligible_candidates_when_success_cap_is_not_met(self) -> None:
        prepared = prepare_run(
            question="Failed fetches should not consume the success cap",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        (run_dir / "sources").mkdir(exist_ok=True)
        page = run_dir / "sources" / "exhaust-page.html"
        page.write_text(
            "<html><body>"
            + "".join(
                f'<img src="https://img.example.test/fail-{index}.txt" alt="{index}">'
                for index in range(4)
            )
            + "</body></html>",
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_exhaust_page",
                "type": "web",
                "url": "https://example.test/exhaust/",
                "title": "Exhaust page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/exhaust-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "visual_required",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        calls: list[str] = []

        def transport(url: str) -> FetchResponse:
            calls.append(url)
            return FetchResponse(
                content=b"not an image",
                mime_type="text/plain",
                status_code=200,
                final_url=url,
            )

        result = extract_and_fetch_page_images(run=run_dir, transport=transport, max_fetches=2)

        self.assertEqual(len(calls), 4)
        self.assertEqual(result["images_linked"], 0)
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        statuses = [record["fetch_status"] for record in fetches]
        self.assertEqual(statuses.count("unsupported_mime"), 4)
        self.assertEqual(statuses.count("budget_pruned"), 0)

    def test_duplicate_failed_url_is_attempted_once_and_deduped(self) -> None:
        prepared = prepare_run(
            question="Duplicate failed image URL should not be retried",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        (run_dir / "sources").mkdir(exist_ok=True)
        image_url = "https://img.example.test/fails-once.png"
        page = run_dir / "sources" / "duplicate-fail-page.html"
        page.write_text(
            "<html><body>"
            + "".join(f'<img src="{image_url}" alt="Duplicate {index}">' for index in range(3))
            + "</body></html>",
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_duplicate_fail_page",
                "type": "web",
                "url": "https://example.test/duplicate-fail/",
                "title": "Duplicate failed page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/duplicate-fail-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "visual_required",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        calls: list[str] = []

        def transport(url: str) -> FetchResponse:
            calls.append(url)
            return FetchResponse(
                content=None,
                mime_type=None,
                status_code=503,
                final_url=url,
                error_code="http_503",
            )

        result = extract_and_fetch_page_images(run=run_dir, transport=transport)

        self.assertEqual(calls, [image_url])
        self.assertEqual(result["images_linked"], 0)
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        self.assertEqual([record["fetch_status"] for record in fetches], ["failed", "deduped", "deduped"])
        first_fetch_id = fetches[0]["fetch_id"]
        for record in fetches[1:]:
            self.assertEqual(record["failure_code"], "duplicate_image_url")
            self.assertEqual(record["dedupe_target_fetch_id"], first_fetch_id)

    def test_data_url_decoding_is_capped_by_max_image_bytes(self) -> None:
        prepared = prepare_run(
            question="Data URL image should be capped",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        (run_dir / "sources").mkdir(exist_ok=True)
        payload = base64.b64encode(PNG_1X1 + b"x" * 1024).decode("ascii")
        page = run_dir / "sources" / "data-page.html"
        page.write_text(
            f'<html><body><img src="data:image/png;base64,{payload}" alt="Large data"></body></html>',
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_data_page",
                "type": "web",
                "url": "https://example.test/data/",
                "title": "Data page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/data-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "visual_required",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        result = extract_and_fetch_page_images(run=run_dir, max_image_bytes=32)

        self.assertEqual(result["images_linked"], 0)
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        self.assertEqual(fetches[0]["fetch_status"], "too_large")
        self.assertEqual(fetches[0]["byte_size"], 33)

    def test_declared_image_mime_requires_image_signature(self) -> None:
        prepared = prepare_run(
            question="Declared MIME alone should not promote invalid image bytes",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        (run_dir / "sources").mkdir(exist_ok=True)
        page = run_dir / "sources" / "invalid-image-page.html"
        page.write_text(
            '<html><body><img src="data:image/png,not-an-image" alt="Invalid"></body></html>',
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_invalid_image_page",
                "type": "web",
                "url": "https://example.test/invalid/",
                "title": "Invalid image page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/invalid-image-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "visual_required",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        result = extract_and_fetch_page_images(run=run_dir)

        self.assertEqual(result["images_linked"], 0)
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        self.assertEqual(fetches[0]["fetch_status"], "unsupported_mime")
        self.assertEqual(fetches[0]["mime_type"], "application/octet-stream")

    def test_mixed_policy_blocked_and_budget_pruned_status_is_budget_pruned(self) -> None:
        prepared = prepare_run(
            question="Mixed blocked and budget-pruned images should be a budget terminal",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        (run_dir / "sources").mkdir(exist_ok=True)
        blocked_page = run_dir / "sources" / "blocked-page.html"
        blocked_page.write_text(
            '<html><body><img src="https://img.example.test/blocked.png" alt="Blocked"></body></html>',
            encoding="utf-8",
        )
        allowed_page = run_dir / "sources" / "allowed-page.html"
        allowed_page.write_text(
            '<html><body><img src="https://img.example.test/allowed.png" alt="Allowed"></body></html>',
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_blocked_page",
                "type": "web",
                "url": "https://example.test/blocked/",
                "title": "Blocked page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "secondary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/blocked-page.html",
                "license_policy": "allowed",
                "robots_policy": "disallowed",
                "policy_decision": "blocked",
                "policy_flags": ["robots_disallowed"],
                "angle_id": "angle_001",
                "route": "visual_required",
            },
            {
                "id": "src_allowed_page",
                "type": "web",
                "url": "https://example.test/allowed/",
                "title": "Allowed page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/allowed-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "visual_required",
            },
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        result = extract_and_fetch_page_images(run=run_dir, max_fetches=0)

        self.assertEqual(result["images_linked"], 0)
        provider_status = self.read_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME)
        self.assertEqual(provider_status["status"], "budget_pruned_visual")
        self.assertFalse(provider_status["ok"])
        self.assertTrue(provider_status["terminal"])
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        self.assertEqual(
            {record["fetch_status"] for record in fetches},
            {"policy_blocked", "budget_pruned"},
        )

    def test_default_budget_respects_zero_image_cap_without_fetching(self) -> None:
        prepared = prepare_run(
            question="Text-only page should not fetch images",
            runs_dir=self.temp_runs_dir(),
            route="text_only",
        )
        run_dir = Path(prepared["run_dir"])
        (run_dir / "sources").mkdir(exist_ok=True)
        page = run_dir / "sources" / "text-page.html"
        page.write_text(
            '<html><body><img src="https://img.example.test/nope.png" alt="Nope"></body></html>',
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_text_page",
                "type": "web",
                "url": "https://example.test/text/",
                "title": "Text page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "secondary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/text-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "text_only",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        calls: list[str] = []

        def transport(url: str) -> FetchResponse:
            calls.append(url)
            return FetchResponse(content=PNG_1X1, mime_type="image/png", status_code=200)

        result = extract_and_fetch_page_images(run=run_dir, transport=transport)

        self.assertEqual(calls, [])
        self.assertEqual(result["images_linked"], 0)
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        self.assertEqual(fetches[0]["fetch_status"], "budget_pruned")

    def test_default_budget_uses_aggregate_image_cap(self) -> None:
        prepared = prepare_run(
            question="Aggregate budget should allow more than one route cap",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            angles=["one", "two", "three"],
        )
        run_dir = Path(prepared["run_dir"])
        (run_dir / "sources").mkdir(exist_ok=True)
        page = run_dir / "sources" / "aggregate-page.html"
        page.write_text(
            "<html><body>"
            + "".join(
                f'<img src="https://img.example.test/aggregate-{index}.png" alt="{index}">'
                for index in range(5)
            )
            + "</body></html>",
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_aggregate_page",
                "type": "web",
                "url": "https://example.test/aggregate/",
                "title": "Aggregate page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/aggregate-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "visual_required",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        def transport(url: str) -> FetchResponse:
            return FetchResponse(
                content=PNG_1X1 + url.encode("utf-8"),
                mime_type="image/png",
                status_code=200,
                final_url=url,
            )

        result = extract_and_fetch_page_images(run=run_dir, transport=transport)

        evidence = self.read_json(run_dir / "evidence.json")
        self.assertEqual(evidence["page_image_extraction"]["max_fetches"], 12)
        self.assertEqual(result["images_linked"], 5)

    def test_cli_acquire_visual_page_image_extractor_provider_fetches_page_images(self) -> None:
        prepared = prepare_run(
            question="CLI page image extraction provider should fetch page images",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        (run_dir / "sources").mkdir(exist_ok=True)
        payload = base64.b64encode(PNG_1X1 + b"cli").decode("ascii")
        page = run_dir / "sources" / "cli-page.html"
        page.write_text(
            f'<html><body><img src="data:image/png;base64,{payload}" alt="CLI image"></body></html>',
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_cli_page",
                "type": "fixture",
                "url": page.resolve().as_uri(),
                "title": "CLI page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/cli-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "angle_id": "angle_001",
                "route": "visual_required",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        command = subprocess.run(
            [
                str(RUNNER),
                "acquire-visual",
                "--run",
                str(run_dir),
                "--provider",
                "page-image-extractor",
                "--max-fetches",
                "1",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(command.returncode, 0, command.stderr)
        payload = json.loads(command.stdout)
        self.assertEqual(payload["status"], "page_images_processed")
        self.assertEqual(payload["images_linked"], 1)
        self.assertTrue((run_dir / VISUAL_CANDIDATES_FILENAME).is_file())
        self.assertTrue((run_dir / IMAGE_FETCH_STATUS_FILENAME).is_file())
        self.assertTrue((run_dir / "page_image_extraction_status.json").is_file())
        evidence = self.read_json(run_dir / "evidence.json")
        self.assertEqual(evidence["images"][0]["provider"], "page-image-extractor")


if __name__ == "__main__":
    unittest.main()
