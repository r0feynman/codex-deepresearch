from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch import (  # noqa: E402
    acquire_visual_candidates,
    ingest_vision_observations,
    prepare_run,
    real_automatic_visual_release_counts,
    validate_artifacts,
    validate_visual_artifacts,
)
from deepresearch.browser_screenshot import BrowserScreenshotCapture  # noqa: E402
from deepresearch import pdf_rasterizer  # noqa: E402
from deepresearch import visual_acquisition  # noqa: E402

prepare_search_handoff_run = prepare_run


def prepare_run(*args, **kwargs):
    kwargs.setdefault("angles", ["primary source discovery"])
    return prepare_search_handoff_run(*args, **kwargs)


class PdfRasterizerTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def read_jsonl(self, path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def png_dimensions(self, path: Path) -> tuple[int, int]:
        data = path.read_bytes()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(data[12:16], b"IHDR")
        return struct.unpack(">II", data[16:24])

    def png_pixel(self, path: Path, x: int, y: int) -> tuple[int, int, int]:
        from PIL import Image

        with Image.open(path) as image:
            return image.convert("RGB").getpixel((x, y))

    def write_pdf(self, run_dir: Path, name: str, body: bytes = b"", pages: int = 3) -> Path:
        path = run_dir / "sources" / name
        path.parent.mkdir(exist_ok=True)
        page_ids = list(range(3, 3 + pages))
        font_id = 3 + pages
        content_ids = list(range(font_id + 1, font_id + 1 + pages))
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            (
                b"<< /Type /Pages /Kids ["
                + b" ".join(f"{page_id} 0 R".encode("ascii") for page_id in page_ids)
                + f"] /Count {pages} >>".encode("ascii")
            ),
        ]
        for page_id, content_id in zip(page_ids, content_ids):
            objects.append(
                (
                    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                    + f"/Resources << /Font << /F1 {font_id} 0 R >> >> ".encode("ascii")
                    + f"/Contents {content_id} 0 R >>".encode("ascii")
                )
            )
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        for page_index in range(1, pages + 1):
            content = (
                b"q\n"
                b"1 0 0 rg\n72 620 220 80 re f\n"
                b"0 0 1 rg\n72 500 320 50 re f\n"
                + f"0 0 0 rg\nBT /F1 32 Tf 72 740 Td (PDF FIXTURE {page_index}) Tj ET\n".encode("ascii")
                + b"BT /F1 18 Tf 72 585 Td (REAL PAGE PIXELS) Tj ET\n"
                b"Q\n"
            )
            objects.append(
                b"<< /Length "
                + str(len(content)).encode("ascii")
                + b" >>\nstream\n"
                + content
                + b"endstream"
            )
        pdf = b"%PDF-1.4\n"
        offsets = [0]
        for index, obj in enumerate(objects, start=1):
            offsets.append(len(pdf))
            pdf += f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
        xref_offset = len(pdf)
        pdf += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")
        for offset in offsets[1:]:
            pdf += f"{offset:010d} 00000 n \n".encode("ascii")
        pdf += (
            f"trailer << /Root 1 0 R /Size {len(objects) + 1} >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
        path.write_bytes(pdf + body)
        return path

    def pdf_source(self, run_dir: Path, source_id: str, pdf_path: Path, **overrides: object) -> dict:
        source = {
            "id": source_id,
            "type": "pdf",
            "url": f"https://example.com/{pdf_path.name}",
            "title": f"PDF fixture {source_id}",
            "published_at": None,
            "accessed_at": "2026-06-25T00:00:00Z",
            "quality": "primary",
            "retrieval_status": "fetched",
            "local_artifact_path": pdf_path.relative_to(run_dir).as_posix(),
            "license_policy": "allowed",
            "robots_policy": "allowed",
            "policy_decision": "allowed",
            "policy_flags": [],
            "route": "visual_required",
            "angle_id": "angle_001",
            "search_result_id": f"sr_{source_id}",
            "pdf_pages": [1],
        }
        source.update(overrides)
        return source

    @unittest.skipUnless(pdf_rasterizer.pdf_renderer_available(), "optional PDF renderer unavailable")
    def test_allowed_pdf_pages_and_figures_link_to_visual_evidence(self) -> None:
        runs_dir = self.temp_runs_dir()
        prepared = prepare_run(
            question="Extract public PDF chart figures",
            runs_dir=runs_dir,
            route="visual_required",
            max_images=4,
        )
        run_dir = Path(prepared["run_dir"])
        pdf_path = self.write_pdf(run_dir, "sample-public.pdf")
        source = self.pdf_source(
            run_dir,
            "src_pdf_public",
            pdf_path,
            pdf_pages=[1],
            figure_hints=[
                {
                    "page_number": 2,
                    "label": "Figure 1",
                    "caption": "Public-safe fixture chart",
                }
            ],
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [source]
        evidence["budget"]["max_images"] = 4
        self.write_json(run_dir / "evidence.json", evidence)

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["local-pdf-rasterizer"],
        )

        self.assertEqual(result["status"], "real_image_search_candidates_collected")
        self.assertTrue(result["validation"]["valid"], result["validation"]["errors"])
        self.assertTrue(result["visual_artifact_validation"]["valid"], result["visual_artifact_validation"]["errors"])
        self.assertEqual(result["candidate_counts"], {"pdf_page": 1, "pdf_figure": 1})
        self.assertEqual(result["pdf_rasterization"]["pages_rasterized"], 2)

        plan = self.read_json(run_dir / "visual_search_plan.json")
        self.assertEqual(plan["tasks"][0]["target_evidence_type"], "pdf_figure")
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        self.assertEqual({candidate["origin"] for candidate in candidates}, {"pdf_page", "pdf_figure"})
        for candidate in candidates:
            self.assertEqual(candidate["provider_mode"], "real")
            self.assertFalse(candidate["provider_provenance"]["fixture_only"])
            self.assertFalse(candidate["provider_provenance"]["external_network_call"])
            artifact = run_dir / candidate["local_artifact_path"]
            self.assertEqual(
                self.png_dimensions(artifact),
                (candidate["width"], candidate["height"]),
            )
            self.assertNotEqual((candidate["width"], candidate["height"]), (1, 1))
            self.assertGreater(artifact.stat().st_size, 1_000)
            red_pixel = self.png_pixel(artifact, 220, 350)
            self.assertGreater(red_pixel[0], 180)
            self.assertLess(red_pixel[1], 90)
            self.assertLess(red_pixel[2], 90)
            self.assertEqual(candidate["rasterizer"]["optional_raster_library"], "pypdfium2")
            self.assertTrue(candidate["rasterizer"]["optional_raster_library_available"])
        self.assertEqual({fetch["fetch_status"] for fetch in fetches}, {"fetched"})
        self.assertTrue(all(fetch["hash"].startswith("sha256:") for fetch in fetches))
        self.assertTrue(all(fetch["pdf_url"] == source["url"] for fetch in fetches))
        self.assertEqual(
            next(fetch for fetch in fetches if fetch.get("figure_hint"))["figure_hint"]["label"],
            "Figure 1",
        )
        self.assertEqual({fetch["provider_mode"] for fetch in fetches}, {"real"})
        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        provider = provider_status["providers"][0]
        self.assertEqual(provider["provider"], "local-pdf-rasterizer")
        self.assertEqual(provider["provider_mode"], "real")
        self.assertEqual(provider["artifacts_fetched"], 2)

        ingest = ingest_vision_observations(run=run_dir, provider="codex-interactive")
        self.assertEqual(ingest["status"], "visual_evidence_ingested")
        validation = validate_artifacts(
            evidence_path=run_dir / "evidence.json",
            visual_observations_path=run_dir / "visual_observations.jsonl",
        )
        self.assertTrue(validation.valid, [error.to_dict() for error in validation.errors])

        evidence = self.read_json(run_dir / "evidence.json")
        self.assertEqual({image["origin"] for image in evidence["images"]}, {"pdf_page", "pdf_figure"})
        self.assertEqual({image["provider_mode"] for image in evidence["images"]}, {"real"})
        self.assertTrue(
            all(not image["provider_provenance"]["fixture_only"] for image in evidence["images"])
        )
        figure = next(image for image in evidence["images"] if image["origin"] == "pdf_figure")
        self.assertEqual(figure["page_number"], 2)
        self.assertEqual(figure["figure_hint"]["label"], "Figure 1")
        self.assertEqual(figure["provider_kind"], "pdf_rasterizer")
        self.assertEqual(figure["pdf_url"], source["url"])
        self.assertEqual(figure["pdf_local_path"], source["local_artifact_path"])
        self.assertTrue(figure["hash"].startswith("sha256:"))
        observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
        self.assertEqual({item["provider_mode"] for item in observations}, {"real"})
        figure_observation = next(item for item in observations if item["origin"] == "pdf_figure")
        self.assertEqual(figure_observation["pdf_url"], figure["pdf_url"])
        self.assertEqual(figure_observation["pdf_local_path"], figure["pdf_local_path"])
        self.assertEqual(figure_observation["page_number"], figure["page_number"])
        self.assertEqual(figure_observation["figure_hint"], figure["figure_hint"])
        self.assertEqual(figure_observation["rasterizer"], figure["rasterizer"])
        self.assertEqual(figure_observation["compute_counters"], figure["compute_counters"])
        self.assertEqual(figure_observation["cost_counters"], figure["cost_counters"])
        counts = real_automatic_visual_release_counts(
            candidates=candidates,
            fetches=fetches,
            observations=observations,
            visual_provider_status=provider_status,
        )
        self.assertEqual(counts["real_candidates"], 2)
        self.assertEqual(counts["real_fetches"], 2)
        self.assertEqual(counts["real_observations"], 2)
        self.assertEqual(counts["real_artifacts_fetched"], 2)

    @unittest.skipUnless(pdf_rasterizer.pdf_renderer_available(), "optional PDF renderer unavailable")
    def test_default_fixture_stack_pdf_records_do_not_count_as_real(self) -> None:
        prepared = prepare_run(
            question="Default fixture stack should not count PDF rasterization as real",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            max_images=20,
        )
        run_dir = Path(prepared["run_dir"])
        pdf_path = self.write_pdf(run_dir, "default-fixture-public.pdf")
        source = self.pdf_source(run_dir, "src_pdf_default_fixture", pdf_path)
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [source]
        evidence["budget"]["max_images"] = 20
        self.write_json(run_dir / "evidence.json", evidence)

        result = acquire_visual_candidates(run=run_dir)

        self.assertEqual(result["status"], "visual_candidates_collected")
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        pdf_candidates = [
            candidate for candidate in candidates if candidate["provider"] == "local-pdf-rasterizer"
        ]
        pdf_fetches = [
            fetch for fetch in fetches if fetch["provider"] == "local-pdf-rasterizer"
        ]
        pdf_observations = [
            observation
            for observation in observations
            if observation["provider"] == "local-pdf-rasterizer"
        ]
        self.assertEqual(len(pdf_candidates), 1)
        self.assertEqual(len(pdf_fetches), 1)
        self.assertEqual(len(pdf_observations), 1)
        self.assertEqual(pdf_candidates[0]["provider_mode"], "fixture")
        self.assertTrue(pdf_candidates[0]["provider_provenance"]["fixture_only"])
        self.assertEqual(pdf_fetches[0]["provider_mode"], "fixture")
        self.assertEqual(pdf_observations[0]["provider_mode"], "fixture")
        self.assertTrue(pdf_observations[0]["provider_provenance"]["fixture_only"])
        provider = next(
            provider
            for provider in provider_status["providers"]
            if provider["provider"] == "local-pdf-rasterizer"
        )
        self.assertEqual(provider["provider_mode"], "fixture")
        self.assertEqual(provider["provider_kind"], "pdf_rasterizer")
        self.assertEqual(provider["artifacts_fetched"], 1)

        counts = real_automatic_visual_release_counts(
            candidates=candidates,
            fetches=fetches,
            observations=observations,
            visual_provider_status=provider_status,
        )
        self.assertEqual(counts["real_candidates"], 0)
        self.assertEqual(counts["real_fetches"], 0)
        self.assertEqual(counts["real_observations"], 0)
        self.assertEqual(counts["real_artifacts_fetched"], 0)

    def test_renderer_unavailable_prevents_accepted_pdf_evidence(self) -> None:
        prepared = prepare_run(
            question="Renderer unavailable PDF rasterization",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            max_images=2,
        )
        run_dir = Path(prepared["run_dir"])
        pdf_path = self.write_pdf(run_dir, "renderer-unavailable.pdf")
        source = self.pdf_source(run_dir, "src_renderer_unavailable", pdf_path)
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [source]
        self.write_json(run_dir / "evidence.json", evidence)

        with mock.patch.object(pdf_rasterizer, "_load_pdfium", return_value=None):
            result = acquire_visual_candidates(
                run=run_dir,
                providers=["local-pdf-rasterizer"],
            )

        self.assertEqual(result["selected_observations"], 0)
        self.assertEqual(result["pdf_rasterization"]["pages_rasterized"], 0)
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        self.assertEqual(fetches[0]["fetch_status"], "failed")
        self.assertEqual(fetches[0]["failure_code"], "renderer_unavailable_pdf")
        self.assertEqual(fetches[0]["provider_mode"], "manual")
        self.assertFalse(fetches[0]["provider_provenance"]["fixture_only"])
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        self.assertEqual(candidates[0]["provider_mode"], "manual")
        self.assertFalse(candidates[0]["provider_provenance"]["fixture_only"])
        provider = self.read_json(run_dir / "visual_provider_status.json")["providers"][0]
        self.assertFalse(provider["available"])
        self.assertEqual(provider["blocked_reason"], "renderer_unavailable_pdf")
        self.assertEqual(provider["provider_mode"], "manual")
        counts = real_automatic_visual_release_counts(
            candidates=candidates,
            fetches=fetches,
            observations=self.read_jsonl(run_dir / "visual_observations.jsonl"),
            visual_provider_status=self.read_json(run_dir / "visual_provider_status.json"),
        )
        self.assertEqual(counts["real_candidates"], 0)
        self.assertEqual(counts["real_fetches"], 0)
        self.assertEqual(counts["real_observations"], 0)
        self.assertEqual(counts["real_artifacts_fetched"], 0)
        evidence = self.read_json(run_dir / "evidence.json")
        self.assertEqual(evidence["images"], [])
        self.assertFalse((run_dir / "images" / "pdf").exists())

    def test_mixed_real_browser_and_pdf_keeps_pdf_diagnostics_active(self) -> None:
        class FakeBrowserTransport:
            name = "fake-browser"
            provider_mode = "real"

            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def availability(self) -> tuple[bool, str | None]:
                return True, None

            def capture(
                self,
                *,
                url: str,
                output_path: Path,
                viewport: dict,
                full_page: bool,
                timeout_ms: int,
            ) -> BrowserScreenshotCapture:
                self.calls.append(
                    {
                        "url": url,
                        "output_path": output_path,
                        "viewport": dict(viewport),
                        "full_page": full_page,
                        "timeout_ms": timeout_ms,
                    }
                )
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(visual_acquisition.PNG_1X1 + b"\nmixed-browser\n")
                return BrowserScreenshotCapture(
                    width=int(viewport["width"]),
                    height=int(viewport["height"]),
                    http_status=200,
                    final_url=url,
                    external_network_call=False,
                    provider_metadata={"fixture": "mixed-real-browser"},
                )

        prepared = prepare_run(
            question="Mixed browser screenshot and PDF rasterization",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            max_images=4,
        )
        run_dir = Path(prepared["run_dir"])
        pdf_path = self.write_pdf(run_dir, "mixed-provider.pdf")
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_browser_mixed",
                "type": "web",
                "url": "https://example.test/mixed-browser",
                "title": "Mixed browser page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/mixed-browser.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "route": "visual_required",
                "angle_id": "angle_001",
            },
            self.pdf_source(run_dir, "src_pdf_mixed", pdf_path),
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        browser_transport = FakeBrowserTransport()

        with mock.patch.object(pdf_rasterizer, "_load_pdfium", return_value=None):
            result = acquire_visual_candidates(
                run=run_dir,
                providers=["browser-screenshot", "local-pdf-rasterizer"],
                screenshot_modes=["first_viewport"],
                browser_transport=browser_transport,
            )

        self.assertEqual(result["status"], "real_image_search_candidates_collected")
        self.assertEqual(len(browser_transport.calls), 1)
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        self.assertEqual(
            {candidate["provider"] for candidate in candidates},
            {"browser-screenshot", "local-pdf-rasterizer"},
        )
        browser_candidates = [
            candidate for candidate in candidates if candidate["provider"] == "browser-screenshot"
        ]
        pdf_candidates = [
            candidate for candidate in candidates if candidate["provider"] == "local-pdf-rasterizer"
        ]
        self.assertEqual(len(browser_candidates), 1)
        self.assertEqual(len(pdf_candidates), 1)
        self.assertEqual(browser_candidates[0]["candidate_status"], "fetched")
        self.assertEqual(browser_candidates[0]["provider_mode"], "real")
        self.assertTrue((run_dir / browser_candidates[0]["local_artifact_path"]).is_file())
        self.assertEqual(pdf_candidates[0]["provider_kind"], "pdf_rasterizer")
        self.assertEqual(pdf_candidates[0]["provider_mode"], "manual")
        self.assertEqual(pdf_candidates[0]["candidate_status"], "fetch_failed")
        self.assertEqual(pdf_candidates[0]["rejection_reason"], "renderer_unavailable_pdf")
        self.assertEqual(
            pdf_candidates[0]["pdf_diagnostic"]["reason"],
            "renderer_unavailable_pdf",
        )

        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        fetch_by_provider = {fetch["provider"]: fetch for fetch in fetches}
        self.assertEqual(fetch_by_provider["browser-screenshot"]["fetch_status"], "fetched")
        self.assertEqual(fetch_by_provider["local-pdf-rasterizer"]["fetch_status"], "failed")
        self.assertEqual(
            fetch_by_provider["local-pdf-rasterizer"]["failure_code"],
            "renderer_unavailable_pdf",
        )

        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        providers = {provider["provider"]: provider for provider in provider_status["providers"]}
        self.assertEqual(set(providers), {"browser-screenshot", "local-pdf-rasterizer"})
        self.assertEqual(providers["browser-screenshot"]["provider_mode"], "real")
        self.assertEqual(providers["browser-screenshot"]["artifacts_fetched"], 1)
        self.assertEqual(providers["local-pdf-rasterizer"]["provider_kind"], "pdf_rasterizer")
        self.assertEqual(providers["local-pdf-rasterizer"]["provider_mode"], "manual")
        self.assertFalse(providers["local-pdf-rasterizer"]["available"])
        self.assertEqual(
            providers["local-pdf-rasterizer"]["blocked_reason"],
            "renderer_unavailable_pdf",
        )
        self.assertEqual(providers["local-pdf-rasterizer"]["pdf_pages_skipped"], 1)
        self.assertEqual(providers["local-pdf-rasterizer"]["artifacts_fetched"], 0)
        self.assertEqual(
            result["pdf_rasterization"]["diagnostics"][0]["failure_code"],
            "renderer_unavailable_pdf",
        )

    @unittest.skipUnless(pdf_rasterizer.pdf_renderer_available(), "optional PDF renderer unavailable")
    def test_pdf_render_failure_records_diagnostics_without_crashing(self) -> None:
        prepared = prepare_run(
            question="PDF render failure diagnostics",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            max_images=2,
        )
        run_dir = Path(prepared["run_dir"])
        pdf_path = self.write_pdf(run_dir, "render-failure.pdf")
        source = self.pdf_source(run_dir, "src_render_failure", pdf_path)
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [source]
        self.write_json(run_dir / "evidence.json", evidence)

        with mock.patch.object(
            visual_acquisition,
            "render_pdf_candidate_artifact",
            side_effect=RuntimeError("deterministic render failure"),
        ):
            result = acquire_visual_candidates(
                run=run_dir,
                providers=["local-pdf-rasterizer"],
            )

        self.assertEqual(result["status"], "partial_auto_visual")
        self.assertEqual(result["selected_observations"], 0)
        self.assertTrue((run_dir / "visual_candidates.jsonl").is_file())
        self.assertTrue((run_dir / "image_fetch_status.jsonl").is_file())
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["candidate_status"], "fetch_failed")
        self.assertEqual(candidate["rejection_reason"], "render_failed_pdf")
        self.assertEqual(candidate["removal_reasons"], ["render_failed_pdf"])
        self.assertIsNone(candidate.get("local_artifact_path"))
        self.assertIsNone(candidate.get("image_url"))
        self.assertIsNone(candidate.get("hash"))
        self.assertEqual(candidate["pdf_diagnostic"]["reason"], "render_failed_pdf")
        self.assertEqual(candidate["pdf_diagnostic"]["error_type"], "RuntimeError")
        self.assertEqual(candidate["compute_counters"]["pdf_pages_rasterized"], 0)
        self.assertEqual(candidate["compute_counters"]["pdf_pages_skipped"], 1)
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        self.assertEqual(fetches[0]["fetch_status"], "failed")
        self.assertEqual(fetches[0]["failure_code"], "render_failed_pdf")
        self.assertIsNone(fetches[0].get("local_artifact_path"))
        provider = self.read_json(run_dir / "visual_provider_status.json")["providers"][0]
        self.assertEqual(provider["artifacts_fetched"], 0)
        self.assertEqual(provider["pdf_pages_rasterized"], 0)
        self.assertEqual(provider["pdf_pages_skipped"], 1)
        self.assertEqual(provider["last_error"], "render_failed_pdf")
        self.assertEqual(provider["diagnostics"][0]["failure_code"], "render_failed_pdf")
        self.assertEqual(
            result["pdf_rasterization"]["diagnostics"][0]["failure_code"],
            "render_failed_pdf",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        self.assertEqual(evidence["images"], [])
        self.assertFalse(list((run_dir / "images" / "pdf").glob("*.png")))

    def test_pdf_diagnostics_are_explicit_for_blocked_and_unsupported_sources(self) -> None:
        prepared = prepare_run(
            question="Diagnose PDF rasterization blockers",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            max_images=8,
        )
        run_dir = Path(prepared["run_dir"])
        sources_dir = run_dir / "sources"
        blocked_pdf = self.write_pdf(run_dir, "blocked.pdf")
        paywalled_pdf = self.write_pdf(run_dir, "paywalled.pdf")
        encrypted_pdf = self.write_pdf(run_dir, "encrypted.pdf", b"x" * 5000 + b"/Encrypt <<>>\n")
        too_large_pdf = self.write_pdf(run_dir, "too-large.pdf", b"x" * 10_000)
        unsupported_pdf = sources_dir / "unsupported.pdf"
        unsupported_pdf.write_bytes(b"not a pdf\n")

        sources = [
            self.pdf_source(
                run_dir,
                "src_policy_blocked",
                blocked_pdf,
                policy_decision="blocked",
                policy_flags=["robots_disallowed"],
            ),
            self.pdf_source(
                run_dir,
                "src_paywalled",
                paywalled_pdf,
                policy_flags=["paywalled"],
            ),
            self.pdf_source(run_dir, "src_encrypted", encrypted_pdf),
            self.pdf_source(run_dir, "src_too_large", too_large_pdf),
            self.pdf_source(run_dir, "src_unsupported", unsupported_pdf),
        ]
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = sources
        evidence["budget"]["max_images"] = 8
        self.write_json(run_dir / "evidence.json", evidence)

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["local-pdf-rasterizer"],
            max_pdf_bytes=8_000,
        )

        self.assertTrue(result["visual_artifact_validation"]["valid"], result["visual_artifact_validation"]["errors"])
        self.assertTrue(result["validation"]["valid"], result["validation"]["errors"])
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        by_code = {fetch["failure_code"]: fetch for fetch in fetches}
        self.assertEqual(
            set(by_code),
            {
                "policy_blocked_pdf",
                "paywalled_pdf",
                "encrypted_pdf",
                "too_large_pdf",
                "unsupported_pdf",
            },
        )
        self.assertEqual(by_code["policy_blocked_pdf"]["fetch_status"], "policy_blocked")
        self.assertEqual(by_code["paywalled_pdf"]["fetch_status"], "policy_blocked")
        self.assertEqual(by_code["too_large_pdf"]["fetch_status"], "too_large")
        self.assertEqual(by_code["encrypted_pdf"]["fetch_status"], "failed")
        self.assertEqual(by_code["unsupported_pdf"]["fetch_status"], "failed")
        diagnostics = result["pdf_rasterization"]["diagnostics"]
        self.assertEqual({item["failure_code"] for item in diagnostics}, set(by_code))

    def test_file_pdf_outside_run_dir_is_not_read_or_persisted(self) -> None:
        prepared = prepare_run(
            question="Block outside local PDF file URLs",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            max_images=2,
        )
        run_dir = Path(prepared["run_dir"])
        outside_dir = self.temp_runs_dir()
        outside_pdf = outside_dir / "outside.pdf"
        outside_pdf.write_bytes(b"%PDF-1.4\noutside\n%%EOF\n")
        inside_pdf = self.write_pdf(run_dir, "inside-copy.pdf")
        source = self.pdf_source(
            run_dir,
            "src_outside_file_uri",
            inside_pdf,
            url=outside_pdf.resolve().as_uri(),
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [source]
        self.write_json(run_dir / "evidence.json", evidence)

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["local-pdf-rasterizer"],
        )

        self.assertEqual(result["selected_observations"], 0)
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        self.assertEqual(fetches[0]["failure_code"], "local_pdf_outside_run_dir")
        self.assertEqual(fetches[0].get("pdf_url"), "")
        self.assertIsNone(fetches[0].get("pdf_local_path"))
        persisted = (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8")
        persisted += (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(outside_pdf.resolve().as_uri(), persisted)
        self.assertNotIn(str(outside_pdf.resolve()), persisted)

    def test_policy_manual_review_and_access_gates_do_not_rasterize(self) -> None:
        prepared = prepare_run(
            question="Policy and access gated PDFs",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            max_images=10,
        )
        run_dir = Path(prepared["run_dir"])
        manual_license_pdf = self.write_pdf(run_dir, "manual-license.pdf")
        manual_robots_pdf = self.write_pdf(run_dir, "manual-robots.pdf")
        manual_access_pdf = self.write_pdf(run_dir, "manual-access.pdf")
        manual_flag_pdf = self.write_pdf(run_dir, "manual-flag.pdf")
        login_pdf = self.write_pdf(run_dir, "login.pdf")
        captcha_pdf = self.write_pdf(run_dir, "captcha.pdf")
        subscription_pdf = self.write_pdf(run_dir, "subscription.pdf")
        sources = [
            self.pdf_source(
                run_dir,
                "src_manual_license",
                manual_license_pdf,
                license_policy="manual_review",
                pdf_pages=[1, 2, 3],
            ),
            self.pdf_source(
                run_dir,
                "src_manual_robots",
                manual_robots_pdf,
                robots_policy="manual_review",
            ),
            self.pdf_source(
                run_dir,
                "src_manual_access",
                manual_access_pdf,
                access_policy="manual_review",
            ),
            self.pdf_source(
                run_dir,
                "src_manual_flag",
                manual_flag_pdf,
                policy_flags=["manual_review_required"],
            ),
            self.pdf_source(
                run_dir,
                "src_login",
                login_pdf,
                policy_flags=["login-gated"],
            ),
            self.pdf_source(
                run_dir,
                "src_captcha",
                captcha_pdf,
                raw_provider_metadata={"access": "CAPTCHA required"},
            ),
            self.pdf_source(
                run_dir,
                "src_subscription",
                subscription_pdf,
                raw_provider_metadata={"availability": "subscription only"},
            ),
        ]
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = sources
        evidence["budget"]["max_images"] = 10
        self.write_json(run_dir / "evidence.json", evidence)

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["local-pdf-rasterizer"],
        )

        self.assertEqual(result["selected_observations"], 0)
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        reason_counts: dict[str, int] = {}
        status_by_reason: dict[str, set[str]] = {}
        for fetch in fetches:
            reason = fetch["failure_code"]
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            status_by_reason.setdefault(reason, set()).add(fetch["fetch_status"])
        self.assertEqual(reason_counts["policy_manual_review_pdf"], 6)
        self.assertEqual(reason_counts["access_blocked_pdf"], 2)
        self.assertEqual(reason_counts["paywalled_pdf"], 1)
        self.assertEqual(status_by_reason["policy_manual_review_pdf"], {"failed"})
        self.assertEqual(status_by_reason["access_blocked_pdf"], {"policy_blocked"})
        self.assertEqual(status_by_reason["paywalled_pdf"], {"policy_blocked"})
        self.assertEqual(result["pdf_rasterization"]["pages_skipped"], 9)
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        manual_sources = {"src_manual_access", "src_manual_flag"}
        self.assertEqual(
            {
                candidate["source_id"]
                for candidate in candidates
                if candidate["rejection_reason"] == "policy_manual_review_pdf"
            }.intersection(manual_sources),
            manual_sources,
        )
        self.assertFalse((run_dir / "images" / "pdf").exists())

    @unittest.skipUnless(pdf_rasterizer.pdf_renderer_available(), "optional PDF renderer unavailable")
    def test_pdf_adapter_fixture_mode_remains_fixture_only(self) -> None:
        prepared = prepare_run(
            question="Direct fixture-mode PDF rasterizer provenance",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            max_images=2,
        )
        run_dir = Path(prepared["run_dir"])
        pdf_path = self.write_pdf(run_dir, "fixture-mode.pdf")
        source = self.pdf_source(run_dir, "src_pdf_fixture_mode", pdf_path)
        route = {
            "id": "angle_001",
            "modality": "visual_required",
            "max_images": 2,
            "visual_tasks": ["inspect_pdf"],
        }

        result = pdf_rasterizer.collect_pdf_rasterizer_candidates(
            run_dir=run_dir,
            sources=[source],
            routes=[route],
            created_at="2026-06-25T00:00:00Z",
            provider="local-pdf-rasterizer",
            provider_mode="fixture",
        )

        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate["provider_mode"], "fixture")
        self.assertTrue(candidate["provider_provenance"]["fixture_only"])
        self.assertTrue(candidate["provider_provenance"]["optional_raster_library_available"])

    @unittest.skipUnless(pdf_rasterizer.pdf_renderer_available(), "optional PDF renderer unavailable")
    def test_pdf_budget_pruning_records_skipped_pages(self) -> None:
        runs_dir = self.temp_runs_dir()
        prepared = prepare_run(
            question="Budget-pruned PDF page rasterization",
            runs_dir=runs_dir,
            route="visual_required",
            max_images=1,
        )
        run_dir = Path(prepared["run_dir"])
        pdf_path = self.write_pdf(run_dir, "budget.pdf")
        source = self.pdf_source(
            run_dir,
            "src_pdf_budget",
            pdf_path,
            pdf_pages=[1, 2, 3],
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [source]
        evidence["budget"]["max_images"] = 1
        self.write_json(run_dir / "evidence.json", evidence)

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["local-pdf-rasterizer"],
        )

        self.assertEqual(result["selected_observations"], 1)
        self.assertTrue(result["validation"]["valid"], result["validation"]["errors"])
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        self.assertEqual(
            [fetch["fetch_status"] for fetch in fetches],
            ["fetched", "budget_pruned", "budget_pruned"],
        )
        self.assertEqual(
            [fetch["failure_code"] for fetch in fetches],
            [None, "budget_pruned", "budget_pruned"],
        )
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        pruned_pages = [
            candidate["page_number"]
            for candidate in candidates
            if candidate["candidate_status"] == "budget_pruned"
        ]
        self.assertEqual(pruned_pages, [2, 3])
        for candidate in candidates:
            if candidate["candidate_status"] == "budget_pruned":
                self.assertIsNone(candidate.get("local_artifact_path"))
                self.assertIsNone(candidate.get("image_url"))
                self.assertIsNone(candidate.get("hash"))
                self.assertEqual(candidate["compute_counters"]["pdf_pages_rasterized"], 0)
                self.assertEqual(candidate["compute_counters"]["pdf_pages_skipped"], 1)
                self.assertTrue(candidate["rasterizer"]["budget_pruned"])
        rendered_artifacts = list((run_dir / "images" / "pdf").glob("*.png"))
        self.assertEqual(len(rendered_artifacts), 1)
        self.assertEqual(result["pdf_rasterization"]["pages_skipped"], 2)
        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        provider = provider_status["providers"][0]
        self.assertEqual(provider["provider"], "local-pdf-rasterizer")
        self.assertEqual(provider["artifacts_fetched"], 1)
        self.assertEqual(result["providers"][0]["artifacts_fetched"], 1)
        visual_validation = validate_visual_artifacts(run_dir=run_dir, evidence_path=None)
        self.assertTrue(visual_validation.valid, [error.to_dict() for error in visual_validation.errors])


if __name__ == "__main__":
    unittest.main()
