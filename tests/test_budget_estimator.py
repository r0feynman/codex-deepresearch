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

from deepresearch import (  # noqa: E402
    BudgetCaps,
    BudgetEstimateError,
    estimate_budget,
    prepare_run as prepare_search_handoff_run,
    read_trace_records,
    resolve_config,
)
from deepresearch.modality_router import route_angles  # noqa: E402


def prepare_run(*args, **kwargs):
    kwargs.setdefault("_allow_release_ineligible_materialization_for_tests", True)
    return prepare_search_handoff_run(*args, **kwargs)


class BudgetEstimatorTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def strict_json_loads(self, payload: str) -> dict:
        def reject_constant(value: str) -> None:
            raise ValueError(f"non-strict JSON constant: {value}")

        parsed = json.loads(payload, parse_constant=reject_constant)
        self.assertIsInstance(parsed, dict)
        return parsed

    def routing(
        self,
        *,
        question: str,
        budget_preset: str = "standard",
        route: str = "text_only",
        angles: list[str] | None = None,
        mode: str = "codex-plugin",
        search_provider: str = "codex-native",
    ) -> tuple[object, list[dict]]:
        config = resolve_config(
            mode=mode,
            search_provider=search_provider,
            budget_preset=budget_preset,
        )
        decisions = route_angles(
            question=question,
            angles=angles or ["primary source discovery"],
            max_images=config.budget.max_images,
            route_override=route,
        )
        return config, [
            {
                "id": f"angle_{index:03d}",
                "modality": decision.modality,
                "max_images": decision.max_images,
            }
            for index, decision in enumerate(decisions, start=1)
        ]

    def test_estimates_quick_standard_deep_and_exhaustive_presets(self) -> None:
        expected_caps = {
            "quick": (8, 4),
            "standard": (20, 8),
            "deep": (40, 24),
            "exhaustive": (100, 100),
        }

        for preset, (max_sources, max_subagents) in expected_caps.items():
            with self.subTest(preset=preset):
                config, routing = self.routing(
                    question="Compare product screenshots",
                    budget_preset=preset,
                    route="visual_required",
                )
                if preset in {"deep", "exhaustive"}:
                    with self.assertRaises(BudgetEstimateError) as raised:
                        estimate_budget(
                            question="Compare product screenshots",
                            config=config,
                            routing=routing,
                            max_results=200,
                        )
                    self.assertEqual(raised.exception.to_dict()["code"], "budget_confirmation_required")

                estimate = estimate_budget(
                    question="Compare product screenshots",
                    config=config,
                    routing=routing,
                    max_results=200,
                    caps=BudgetCaps(max_cost_usd=999.0) if preset == "exhaustive" else None,
                    confirmation_provided=True,
                )

                self.assertEqual(estimate["preset_caps"]["max_sources"], max_sources)
                self.assertEqual(
                    estimate["preset_caps"]["max_concurrent_codex_subagents"],
                    max_subagents,
                )
                self.assertEqual(estimate["estimates"]["source_count"], max_sources)
                self.assertGreaterEqual(estimate["estimates"]["image_count"], 1)
                self.assertGreater(estimate["estimates"]["verifier_invocation_count"], 0)
                self.assertGreater(estimate["estimates"]["codex_subagent_count"], 0)
                self.assertEqual(estimate["estimates"]["runner_stage_count"], 7)
                self.assertIn("total_input_tokens_placeholder", estimate["estimates"]["token_placeholders"])
                self.assertIn("total_model_api_calls", estimate["estimates"]["model_call_placeholders"])
                self.assertGreater(estimate["high_water_cost_bounds"]["upper_bound_usd"], 0)

    def test_provider_mode_runner_mode_and_concurrency_caps_are_accounted_for(self) -> None:
        config, routing = self.routing(
            question="Find current market data",
            mode="automated-cli",
            search_provider="openai",
            route="text_only",
            angles=["official sources", "recent reports"],
        )

        estimate = estimate_budget(
            question="Find current market data",
            config=config,
            routing=routing,
            max_results=20,
            codex_runner="codex-sdk",
            caps=BudgetCaps(max_subagents=3, max_agents=2),
        )

        self.assertEqual(estimate["search_provider"], "openai")
        self.assertEqual(estimate["codex_runner"], "codex-sdk")
        self.assertEqual(estimate["estimates"]["search_call_count"], 2)
        self.assertEqual(
            estimate["estimates"]["model_call_placeholders"]["provider_api_calls"],
            2,
        )
        self.assertEqual(estimate["estimates"]["codex_subagent_concurrency"], 3)
        self.assertEqual(estimate["estimates"]["runner_agent_concurrency"], 2)
        self.assertEqual(
            estimate["estimates"]["model_call_placeholders"]["runner_orchestration_calls"],
            4,
        )
        self.assertEqual(
            {suggestion["code"] for suggestion in estimate["suggestions"]},
            {"reduce_codex_subagent_concurrency", "reduce_runner_agent_concurrency"},
        )

    def test_user_caps_create_deterministic_reduction_suggestions(self) -> None:
        config, routing = self.routing(
            question="Compare visual evidence",
            route="visual_required",
        )

        estimate = estimate_budget(
            question="Compare visual evidence",
            config=config,
            routing=routing,
            max_results=100,
            caps=BudgetCaps(max_sources=6, max_images=2, max_subagents=4, max_agents=3),
        )

        self.assertEqual(estimate["estimates"]["source_count"], 6)
        self.assertEqual(estimate["estimates"]["image_count"], 2)
        self.assertEqual(estimate["planned_search"]["max_results_per_task"], 6)
        self.assertEqual(estimate["planned_search"]["route_image_allocations"]["angle_001"], 2)
        self.assertEqual(
            [suggestion["code"] for suggestion in estimate["suggestions"]],
            [
                "reduce_sources",
                "reduce_images",
                "reduce_codex_subagent_concurrency",
                "reduce_runner_agent_concurrency",
            ],
        )

    def test_max_cost_cap_reduces_plan_before_execution(self) -> None:
        config, routing = self.routing(
            question="Compare visual evidence",
            route="visual_required",
        )

        estimate = estimate_budget(
            question="Compare visual evidence",
            config=config,
            routing=routing,
            max_results=100,
            caps=BudgetCaps(max_cost_usd=0.40),
        )

        self.assertLessEqual(estimate["high_water_cost_bounds"]["upper_bound_usd"], 0.40)
        self.assertIn(
            "reduce_to_cost_cap",
            {suggestion["code"] for suggestion in estimate["suggestions"]},
        )
        self.assertLess(estimate["estimates"]["source_count"], 20)
        self.assertLessEqual(estimate["estimates"]["image_count"], 12)

    def test_invalid_and_impossible_caps_raise_machine_readable_errors(self) -> None:
        config, routing = self.routing(
            question="Compare visual evidence",
            route="visual_required",
        )

        with self.assertRaises(BudgetEstimateError) as invalid:
            estimate_budget(
                question="Compare visual evidence",
                config=config,
                routing=routing,
                max_results=10,
                caps=BudgetCaps(max_sources=0),
            )
        self.assertEqual(invalid.exception.to_dict()["code"], "invalid_cap")
        self.assertEqual(invalid.exception.to_dict()["field"], "max_sources")
        json.loads(str(invalid.exception))

        with self.assertRaises(BudgetEstimateError) as impossible:
            estimate_budget(
                question="Compare visual evidence",
                config=config,
                routing=routing,
                max_results=10,
                caps=BudgetCaps(max_images=0),
            )
        self.assertEqual(impossible.exception.to_dict()["code"], "impossible_image_cap")
        json.loads(str(impossible.exception))

    def test_non_finite_cost_cap_cli_error_is_strict_json(self) -> None:
        runs_dir = self.temp_runs_dir()
        result = subprocess.run(
            [
                str(RUNNER),
                "prepare",
                "Non-finite cost cap",
                "--angle",
                "primary source discovery",
                "--allow-release-ineligible-materialization-for-tests",
                "--runs-dir",
                str(runs_dir),
                "--max-cost-usd",
                "nan",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        payload = self.strict_json_loads(result.stderr)
        self.assertEqual(payload["code"], "invalid_cap")
        self.assertEqual(payload["field"], "max_cost_usd")
        self.assertEqual(payload["value"], "nan")
        self.assertEqual(list(runs_dir.iterdir()), [])

    def test_max_results_zero_cli_error_is_machine_readable(self) -> None:
        runs_dir = self.temp_runs_dir()
        result = subprocess.run(
            [
                str(RUNNER),
                "prepare",
                "Invalid max results",
                "--runs-dir",
                str(runs_dir),
                "--max-results",
                "0",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        payload = self.strict_json_loads(result.stderr)
        self.assertEqual(payload["code"], "invalid_cap")
        self.assertEqual(payload["field"], "max_results")
        self.assertEqual(payload["value"], 0)
        self.assertEqual(list(runs_dir.iterdir()), [])

    def test_prepare_requires_confirmation_for_deep_budget(self) -> None:
        runs_dir = self.temp_runs_dir()
        result = subprocess.run(
            [
                str(RUNNER),
                "prepare",
                "Deep budget confirmation",
                "--angle",
                "primary source discovery",
                "--allow-release-ineligible-materialization-for-tests",
                "--runs-dir",
                str(runs_dir),
                "--budget",
                "deep",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stderr)
        self.assertEqual(payload["code"], "budget_confirmation_required")
        self.assertEqual(payload["details"]["required_flag"], "--confirm-budget")
        self.assertEqual(list(runs_dir.iterdir()), [])

    def test_prepare_rejects_impossible_visual_cap_without_partial_run(self) -> None:
        runs_dir = self.temp_runs_dir()
        result = subprocess.run(
            [
                str(RUNNER),
                "prepare",
                "Impossible visual cap",
                "--runs-dir",
                str(runs_dir),
                "--route",
                "visual_required",
                "--angle",
                "primary source discovery",
                "--allow-release-ineligible-materialization-for-tests",
                "--max-images",
                "0",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stderr)
        self.assertEqual(payload["code"], "impossible_image_cap")
        self.assertEqual(payload["field"], "max_images")
        self.assertEqual(list(runs_dir.iterdir()), [])

    def test_exhaustive_requires_cost_cap_even_with_confirmation(self) -> None:
        config, routing = self.routing(
            question="Broad exhaustive research",
            budget_preset="exhaustive",
            route="text_only",
        )

        with self.assertRaises(BudgetEstimateError) as raised:
            estimate_budget(
                question="Broad exhaustive research",
                config=config,
                routing=routing,
                max_results=200,
                confirmation_provided=True,
            )
        payload = raised.exception.to_dict()
        self.assertEqual(payload["code"], "budget_cost_cap_required")
        self.assertEqual(payload["field"], "max_cost_usd")
        self.assertEqual(payload["details"]["required_flag"], "--max-cost-usd")

        runs_dir = self.temp_runs_dir()
        result = subprocess.run(
            [
                str(RUNNER),
                "prepare",
                "Exhaustive without cost cap",
                "--angle",
                "primary source discovery",
                "--allow-release-ineligible-materialization-for-tests",
                "--runs-dir",
                str(runs_dir),
                "--budget",
                "exhaustive",
                "--confirm-budget",
                "--max-results",
                "200",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stderr)
        self.assertEqual(payload["code"], "budget_cost_cap_required")
        self.assertEqual(payload["field"], "max_cost_usd")
        self.assertEqual(list(runs_dir.iterdir()), [])

    def test_prepare_writes_and_links_budget_estimate_artifact(self) -> None:
        prepared = prepare_run(
            question="Budget estimate smoke",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            budget_preset="quick",
            max_results=20,
            max_sources=4,
            max_images=2,
            max_subagents=2,
            max_agents=2,
            codex_runner="serial",
            angles=["primary source discovery"],
        )
        run_dir = Path(prepared["run_dir"])

        estimate = json.loads((run_dir / "budget_estimate.json").read_text(encoding="utf-8"))
        status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
        trace = next(
            record
            for record in read_trace_records(run_dir / "run_trace.jsonl")
            if record.get("event_type") == "run_start"
        )
        evidence = json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))
        search_tasks = json.loads((run_dir / "search_tasks.json").read_text(encoding="utf-8"))
        visual_tasks = json.loads((run_dir / "visual_tasks.json").read_text(encoding="utf-8"))

        self.assertEqual(estimate["estimates"]["source_count"], 4)
        self.assertEqual(estimate["estimates"]["image_count"], 2)
        self.assertEqual(prepared["budget_estimate"]["source_count"], 4)
        self.assertIn("model_call_placeholders", prepared["budget_estimate"])
        self.assertIn("token_placeholders", prepared["budget_estimate"])
        self.assertEqual(
            prepared["budget_estimate"]["model_call_placeholders"][
                "total_model_api_calls"
            ],
            estimate["estimates"]["model_call_placeholders"]["total_model_api_calls"],
        )
        self.assertEqual(
            prepared["budget_estimate"]["token_placeholders"][
                "total_input_tokens_placeholder"
            ],
            estimate["estimates"]["token_placeholders"][
                "total_input_tokens_placeholder"
            ],
        )
        self.assertEqual(status["artifacts"]["budget_estimate"], str(run_dir / "budget_estimate.json"))
        self.assertIn("model_call_placeholders", status["budget_estimate"])
        self.assertIn("token_placeholders", status["budget_estimate"])
        self.assertEqual(trace["artifacts"]["budget_estimate"], str(run_dir / "budget_estimate.json"))
        self.assertEqual(evidence["budget"]["max_sources"], 4)
        self.assertEqual(evidence["budget"]["max_images"], 2)
        self.assertEqual(search_tasks["tasks"][0]["max_results"], 4)
        self.assertEqual(visual_tasks["tasks"][0]["max_images"], 2)


if __name__ == "__main__":
    unittest.main()
