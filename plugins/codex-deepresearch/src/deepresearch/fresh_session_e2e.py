"""Fresh-session transcript gate for the DeepResearch skill."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from .visual_artifacts import (
    IMAGE_FETCH_STATUS_FILENAME,
    VISUAL_CANDIDATES_FILENAME,
    VISUAL_PROVIDER_STATUS_FILENAME,
    real_automatic_visual_release_counts,
    validate_visual_artifacts,
    visual_minimums_for_run,
)

FRESH_SESSION_E2E_SCHEMA_VERSION = "codex-deepresearch.fresh-session-e2e.v0"
FRESH_SESSION_VISUAL_E2E_SCHEMA_VERSION = "codex-deepresearch.fresh-session-visual-e2e.v0"
DEFAULT_FRESH_SESSION_INVOKE = (
    "$deep-research: Compare three public approaches for deterministic "
    "software release validation and cite the evidence quality tradeoffs."
)
DEFAULT_FRESH_SESSION_VISUAL_INVOKE = (
    "$deep-research: Compare public product screenshots for three calculator "
    "applications and cite the visual evidence differences."
)
DEFAULT_SCENARIO_TIMEOUT_SECONDS = 120.0
DEFAULT_MIN_REAL_VISUAL_CANDIDATES = 10
REAL_CODEX_EXEC_MODES = ("auto", "require", "skip")
REAL_CODEX_INTERACTIVE_MODES = ("auto", "require", "skip")

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PLUGIN_ROOT.parents[1]
RUNNER_PATH = PLUGIN_ROOT / "scripts" / "codex-deepresearch"
SKILL_PATH = PLUGIN_ROOT / "skills" / "deep-research" / "SKILL.md"

_SYNTHESIZED_SUCCESS_STATUSES = {
    "completed_parallel",
    "completed_partial_parallel",
    "completed_serial_handoff",
    "completed_auto_visual",
    "partial_auto_visual",
    "completed_fixture",
}
_EXPLICIT_TERMINAL_STATUSES = {
    "blocked_preflight",
    "blocked_missing_search_handoff",
    "blocked_parallel_execution",
    "blocked_missing_visual_provider",
    "blocked_missing_vlm_provider",
    "policy_blocked_visual",
    "budget_pruned_visual",
    "failed_parallel_no_accepted_shards",
    "failed_validation",
    "failed_synthesis",
}
_SUCCESS_MARKERS = (
    "completed",
    "complete",
    "successful",
    "succeeded",
    "finished",
    "synthesized",
    "report generated",
    "done",
)


class FreshSessionE2EError(ValueError):
    """Raised when the fresh-session gate fails."""

    def __init__(self, message: str, *, results_path: Path | None = None) -> None:
        super().__init__(message)
        self.results_path = results_path


def run_fresh_session_e2e(
    *,
    runs_dir: str | Path,
    suite_id: str = "fresh-session-e2e",
    invocation: str = DEFAULT_FRESH_SESSION_INVOKE,
    clean: bool = False,
    real_codex_exec: str = "skip",
    runner_path: str | Path = RUNNER_PATH,
    skill_path: str | Path = SKILL_PATH,
    scenario_timeout_seconds: float = DEFAULT_SCENARIO_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run a public-safe scripted transcript gate for a fresh skill invocation."""

    if real_codex_exec not in REAL_CODEX_EXEC_MODES:
        raise FreshSessionE2EError(
            "real_codex_exec must be one of: " + ", ".join(REAL_CODEX_EXEC_MODES)
        )
    if not invocation.startswith("$deep-research:"):
        raise FreshSessionE2EError("fresh-session gate requires a $deep-research: invocation")

    root_runs_dir = Path(runs_dir)
    suite_dir = root_runs_dir / suite_id
    if suite_dir.exists():
        if not clean:
            raise FreshSessionE2EError(f"suite directory already exists: {suite_dir}")
        shutil.rmtree(suite_dir)
    suite_dir.mkdir(parents=True)
    results_path = suite_dir / "fresh_session_e2e_results.json"

    runner = Path(runner_path)
    scenarios: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    skill_transcript_gate = _skill_transcript_gate(
        runner_path=runner,
        skill_path=Path(skill_path),
        invocation=invocation,
    )
    if skill_transcript_gate["status"] != "passed":
        failures.append(
            {
                "check": "skill_transcript_gate",
                "detail": skill_transcript_gate["detail"],
            }
        )

    for scenario in (
        _fixture_scenario(invocation),
        _serial_fallback_scenario(invocation),
        _real_codex_exec_scenario(invocation, real_codex_exec=real_codex_exec),
    ):
        summary = _run_scenario(
            scenario,
            suite_dir=suite_dir,
            runner_path=runner,
            skill_transcript_gate=skill_transcript_gate,
            timeout_seconds=scenario_timeout_seconds,
        )
        scenarios.append(summary)
        failures.extend(summary.get("failures", []))

    acceptance = _acceptance(scenarios)
    runner_artifact_gate = _runner_artifact_gate(scenarios)
    outcome_counts = _outcome_counts(scenarios)
    if runner_artifact_gate["status"] != "passed":
        failures.append(
            {
                "check": "runner_artifact_gate",
                "detail": runner_artifact_gate["detail"],
            }
        )
    for key, value in acceptance.items():
        if value is not True:
            failures.append(
                {
                    "check": key,
                    "detail": "fresh-session E2E acceptance check did not pass",
                }
            )

    results: dict[str, Any] = {
        "schema_version": FRESH_SESSION_E2E_SCHEMA_VERSION,
        "status": "passed" if not failures else "failed",
        "generated_at": _utc_now(),
        "release_gate_passed": (
            not failures and outcome_counts.get("completed_real_parallel", 0) > 0
        ),
        "suite_id": suite_id,
        "suite_dir": str(suite_dir.resolve()),
        "invocation": invocation,
        "real_codex_exec_mode": real_codex_exec,
        "scenario_timeout_seconds": scenario_timeout_seconds,
        "skill_transcript_gate": skill_transcript_gate,
        "runner_artifact_gate": runner_artifact_gate,
        "scenarios": scenarios,
        "outcome_counts": outcome_counts,
        "acceptance": acceptance,
        "failures": failures,
        "artifacts": {"results": str(results_path.resolve())},
        "public_safe": True,
    }
    _write_json(results_path, results)
    if failures:
        raise FreshSessionE2EError(
            f"fresh-session E2E gate failed; see {results_path}",
            results_path=results_path,
        )
    return results


def run_fresh_session_visual_e2e(
    *,
    runs_dir: str | Path,
    suite_id: str = "fresh-session-visual-e2e",
    invocation: str = DEFAULT_FRESH_SESSION_VISUAL_INVOKE,
    clean: bool = False,
    real_codex_interactive: str = "skip",
    completed_auto_visual_run: str | Path | None = None,
    runner_path: str | Path = RUNNER_PATH,
    skill_path: str | Path = SKILL_PATH,
    scenario_timeout_seconds: float = DEFAULT_SCENARIO_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run the fresh-session visual gate without fabricating real visual capability."""

    if real_codex_interactive not in REAL_CODEX_INTERACTIVE_MODES:
        raise FreshSessionE2EError(
            "real_codex_interactive must be one of: "
            + ", ".join(REAL_CODEX_INTERACTIVE_MODES)
        )
    if not invocation.startswith("$deep-research:"):
        raise FreshSessionE2EError("fresh-session visual gate requires a $deep-research: invocation")

    root_runs_dir = Path(runs_dir)
    suite_dir = root_runs_dir / suite_id
    if suite_dir.exists():
        if not clean:
            raise FreshSessionE2EError(f"suite directory already exists: {suite_dir}")
        shutil.rmtree(suite_dir)
    suite_dir.mkdir(parents=True)
    results_path = suite_dir / "fresh_session_visual_e2e_results.json"

    runner = Path(runner_path)
    scenarios: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    skill_transcript_gate = _skill_transcript_gate(
        runner_path=runner,
        skill_path=Path(skill_path),
        invocation=invocation,
    )
    if skill_transcript_gate["status"] != "passed":
        failures.append(
            {
                "check": "skill_transcript_gate",
                "detail": skill_transcript_gate["detail"],
            }
        )

    scenario_specs: list[Mapping[str, Any]] = [
        _visual_fixture_scenario(invocation),
        _visual_public_safe_provider_blocked_scenario(invocation),
    ]
    if completed_auto_visual_run is not None:
        completed_run_status = _load_completed_auto_visual_run_status(
            Path(completed_auto_visual_run)
        )
        scenario_specs.append(
            _visual_completed_auto_visual_scenario(
                completed_run_status,
                invocation=invocation,
            )
        )

    for scenario in scenario_specs:
        summary = _run_scenario(
            scenario,
            suite_dir=suite_dir,
            runner_path=runner,
            skill_transcript_gate=skill_transcript_gate,
            timeout_seconds=scenario_timeout_seconds,
        )
        scenarios.append(summary)
        failures.extend(summary.get("failures", []))

    acceptance = _visual_acceptance(scenarios)
    if real_codex_interactive == "require" and not acceptance["release_gate_passed"]:
        failures.append(
            {
                "check": "visual_release_gate_required",
                "detail": (
                    "real Codex-interactive visual mode was required, but no "
                    "visual-required fresh-session scenario reached completed_auto_visual "
                    "with release-grade evidence"
                ),
            }
        )
    for key in (
        "completed_auto_visual_validation_rules_met",
        "blocked_runs_name_missing_capability",
        "blocked_runs_do_not_count_as_release_passes",
        "fixture_manual_user_evidence_excluded",
        "final_transcript_exposes_artifacts_and_status_summary",
    ):
        if acceptance[key] is not True:
            failures.append(
                {
                    "check": key,
                    "detail": "fresh-session visual E2E invariant did not pass",
                }
            )

    release_gate_status = _visual_release_gate_status(
        scenarios,
        real_codex_interactive=real_codex_interactive,
    )
    results: dict[str, Any] = {
        "schema_version": FRESH_SESSION_VISUAL_E2E_SCHEMA_VERSION,
        "status": "passed" if not failures else "failed",
        "generated_at": _utc_now(),
        "release_gate_status": release_gate_status,
        "release_gate_passed": acceptance["release_gate_passed"],
        "suite_id": suite_id,
        "suite_dir": str(suite_dir.resolve()),
        "invocation": invocation,
        "real_codex_interactive_mode": real_codex_interactive,
        "completed_auto_visual_run": str(Path(completed_auto_visual_run).resolve())
        if completed_auto_visual_run is not None
        else None,
        "scenario_timeout_seconds": scenario_timeout_seconds,
        "skill_transcript_gate": skill_transcript_gate,
        "scenarios": scenarios,
        "outcome_counts": _visual_outcome_counts(scenarios),
        "acceptance": acceptance,
        "failures": failures,
        "artifacts": {"results": str(results_path.resolve())},
        "public_safe": True,
    }
    _write_json(results_path, results)
    if failures:
        raise FreshSessionE2EError(
            f"fresh-session visual E2E gate failed; see {results_path}",
            results_path=results_path,
        )
    return results


def render_final_response(
    run_status: Mapping[str, Any],
    *,
    scenario_id: str,
    skill_transcript_gate: Mapping[str, Any] | None = None,
) -> str:
    """Render the assistant-facing response that the transcript gate validates."""

    artifacts = run_status.get("artifacts") if isinstance(run_status.get("artifacts"), Mapping) else {}
    shard_summary = (
        run_status.get("shard_summary")
        if isinstance(run_status.get("shard_summary"), Mapping)
        else {}
    )
    fallback = run_status.get("fallback") if isinstance(run_status.get("fallback"), Mapping) else {}
    diagnostics = (
        run_status.get("diagnostics") if isinstance(run_status.get("diagnostics"), Mapping) else {}
    )
    provenance = (
        run_status.get("provenance") if isinstance(run_status.get("provenance"), Mapping) else {}
    )
    visual_release_gate = (
        run_status.get("visual_release_gate")
        if isinstance(run_status.get("visual_release_gate"), Mapping)
        else {}
    )
    visual_provider = (
        run_status.get("visual_provider")
        if isinstance(run_status.get("visual_provider"), Mapping)
        else {}
    )
    lines = [
        "DeepResearch fresh-session transcript result",
        "Transcript kind: skill-invocation",
        f"Scenario: {scenario_id}",
        f"Mode: {run_status.get('selected_mode')}",
        f"Status: {run_status.get('status')}",
        f"Run directory: {run_status.get('run_dir') or 'none'}",
        f"Provenance: {_provenance_class(run_status)}",
        (
            "Shard summary: "
            f"accepted_shards={shard_summary.get('accepted_shard_count', 0)}, "
            f"merged_shards={shard_summary.get('merged_shard_count', 0)}, "
            f"failed_tasks={shard_summary.get('failed_task_count', 0)}, "
            f"blocked_tasks={shard_summary.get('blocked_task_count', 0)}"
        ),
        (
            "Fallback: "
            f"parallel_degraded={fallback.get('parallel_degraded')}, "
            f"needs_serial_handoff={fallback.get('needs_serial_handoff')}"
        ),
        (
            "Real execution: "
            f"attempted={provenance.get('attempted_real_child_execution', False)}, "
            f"accepted_shards={provenance.get('accepted_shards', 0)}"
        ),
    ]
    if visual_release_gate:
        lines.append(
            "Visual summary: "
            f"release_gate_passed={visual_release_gate.get('release_gate_passed')}, "
            f"status={visual_release_gate.get('status')}, "
            f"real_candidates={visual_release_gate.get('real_candidate_count', 0)}, "
            f"fetched_artifacts={visual_release_gate.get('real_fetched_artifacts', 0)}, "
            f"codex_interactive_analyzed_images="
            f"{visual_release_gate.get('codex_interactive_analyzed_images', 0)}, "
            f"report_cited_visual_claims="
            f"{visual_release_gate.get('report_cited_visual_or_mixed_claims', 0)}, "
            f"blocked_capability={visual_release_gate.get('blocked_capability')}"
        )
    elif visual_provider:
        lines.append(
            "Visual summary: "
            f"provider_status={visual_provider.get('status')}, "
            f"blocked_capability={_blocked_capability(run_status)}"
        )
    lines.append("Artifacts:")
    if isinstance(skill_transcript_gate, Mapping):
        lines.insert(
            2,
            f"Skill instructions: {skill_transcript_gate.get('skill_path')} "
            f"sha256={skill_transcript_gate.get('skill_sha256')}",
        )
        lines.insert(
            3,
            f"Skill route command: {skill_transcript_gate.get('route_command')}",
        )
    for key in sorted(artifacts):
        value = artifacts[key]
        if isinstance(value, str):
            lines.append(f"- {key}: {value}")
    if diagnostics:
        lines.append(f"Diagnostics: {diagnostics.get('actionable_cause', '')}")
    return "\n".join(lines).rstrip() + "\n"


def validate_final_response(
    *,
    run_status: Mapping[str, Any],
    final_response: str,
    scenario_id: str,
    skill_transcript_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one scripted assistant response against fresh-session rules."""

    failures: list[dict[str, Any]] = []
    status = str(run_status.get("status") or "")
    selected_mode = str(run_status.get("selected_mode") or "")
    run_dir_value = run_status.get("run_dir")
    run_dir = Path(str(run_dir_value)) if isinstance(run_dir_value, str) and run_dir_value else None
    artifacts = run_status.get("artifacts") if isinstance(run_status.get("artifacts"), Mapping) else {}
    provenance = (
        run_status.get("provenance") if isinstance(run_status.get("provenance"), Mapping) else {}
    )
    shard_summary = (
        run_status.get("shard_summary")
        if isinstance(run_status.get("shard_summary"), Mapping)
        else {}
    )

    if selected_mode == "quick-chat":
        failures.append(
            _failure(
                scenario_id,
                "chat_only",
                "fresh-session $deep-research invocation ended as quick-chat",
            )
        )

    if not isinstance(skill_transcript_gate, Mapping) or skill_transcript_gate.get("status") != "passed":
        failures.append(
            _failure(
                scenario_id,
                "skill_transcript_gate_not_passed",
                "fresh-session transcript must be generated from passing canonical skill instructions",
            )
        )
    else:
        required_markers = (
            "Transcript kind: skill-invocation",
            str(skill_transcript_gate.get("skill_path") or ""),
            str(skill_transcript_gate.get("route_command") or ""),
        )
        missing_markers = [
            marker
            for marker in required_markers
            if marker and marker not in final_response
        ]
        if missing_markers:
            failures.append(
                _failure(
                    scenario_id,
                    "not_skill_invocation_transcript",
                    "transcript is missing skill-invocation markers: "
                    + ", ".join(missing_markers),
                )
            )

    response_successful = _looks_successful(final_response)
    if response_successful and not run_dir:
        failures.append(
            _failure(
                scenario_id,
                "success_without_run_dir",
                "final response looks successful but does not expose a run directory",
            )
        )
    if run_dir and str(run_dir) not in final_response:
        failures.append(
            _failure(
                scenario_id,
                "missing_run_dir_in_response",
                "final response does not include the run artifact path",
            )
        )

    if run_dir:
        _require_artifact(
            failures,
            scenario_id=scenario_id,
            artifact_key="run_status",
            artifacts=artifacts,
            run_dir=run_dir,
            final_response=final_response,
        )
    elif status not in _EXPLICIT_TERMINAL_STATUSES:
        failures.append(
            _failure(
                scenario_id,
                "missing_run_dir_without_blocked_status",
                "missing run directory is allowed only for explicit blocked terminal states",
            )
        )

    if _is_synthesized_success(run_status):
        for artifact_key in ("report", "evidence", "run_status", "report_status"):
            _require_artifact(
                failures,
                scenario_id=scenario_id,
                artifact_key=artifact_key,
                artifacts=artifacts,
                run_dir=run_dir,
                final_response=final_response,
            )
        if str(artifacts.get("report_status") or "") and "report_status.json" not in final_response:
            failures.append(
                _failure(
                    scenario_id,
                    "missing_report_status_filename",
                    "synthesized success response must name report_status.json",
                )
            )

    if response_successful and status in _SYNTHESIZED_SUCCESS_STATUSES:
        for artifact_key in ("run_status", "report_status"):
            if artifact_key not in artifacts:
                failures.append(
                    _failure(
                        scenario_id,
                        f"successful_response_missing_{artifact_key}",
                        f"successful-looking response lacks {artifact_key}.json",
                    )
                )

    provenance_class = _provenance_class(run_status)
    if provenance_class == "real_parallel":
        accepted_shards = _int_or_zero(
            provenance.get("accepted_shards", shard_summary.get("accepted_shard_count"))
        )
        if accepted_shards <= 0:
            failures.append(
                _failure(
                    scenario_id,
                    "real_parallel_without_accepted_shards",
                    "real codex-exec execution must record accepted_shards > 0",
                )
            )

    if provenance_class == "serial_fallback" and status not in _EXPLICIT_TERMINAL_STATUSES:
        failures.append(
            _failure(
                scenario_id,
                "serial_fallback_not_blocked",
                "serial fallback must be tracked as an explicit terminal blocked state",
            )
        )

    if status in _EXPLICIT_TERMINAL_STATUSES and "blocked" not in final_response.lower() and "failed" not in final_response.lower():
        failures.append(
            _failure(
                scenario_id,
                "blocked_status_not_visible",
                "blocked or failed terminal status must be visible in the final response",
            )
        )

    return {
        "status": "passed" if not failures else "failed",
        "scenario_id": scenario_id,
        "failures": failures,
        "provenance_class": provenance_class,
        "terminal_outcome": _terminal_outcome(run_status),
        "required_artifacts": _required_artifacts(run_status),
    }


def _run_scenario(
    scenario: Mapping[str, Any],
    *,
    suite_dir: Path,
    runner_path: Path,
    skill_transcript_gate: Mapping[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    scenario_id = str(scenario["id"])
    transcript_path = suite_dir / f"{scenario_id}_transcript.md"
    command = _scenario_command_args(
        scenario,
        scenario_runs_dir=suite_dir / scenario_id,
    )
    if command is None:
        run_status = dict(scenario["run_status"])
        completed = None
        timed_out = False
    else:
        try:
            completed = subprocess.run(
                [str(runner_path), *command],
                cwd=REPO_ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            run_status = _payload_from_subprocess(completed)
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            completed = None
            timed_out = True
            run_status = _timeout_run_status(
                scenario=scenario,
                command=[str(runner_path), *command],
                timeout_seconds=timeout_seconds,
                exc=exc,
            )

    response_run_status = run_status
    visual_release_gate: dict[str, Any] | None = None
    if scenario.get("visual_release_gate") is True:
        visual_release_gate = _visual_release_gate(
            run_status,
            final_response="",
            scenario_id=scenario_id,
            check_final_response=False,
        )
        response_run_status = dict(run_status)
        response_run_status["visual_release_gate"] = _visual_release_summary(
            visual_release_gate
        )

    final_response = render_final_response(
        response_run_status,
        scenario_id=scenario_id,
        skill_transcript_gate=skill_transcript_gate,
    )
    if scenario.get("visual_release_gate") is True:
        visual_release_gate = _visual_release_gate(
            run_status,
            final_response=final_response,
            scenario_id=scenario_id,
            check_final_response=True,
        )
        response_run_status = dict(run_status)
        response_run_status["visual_release_gate"] = _visual_release_summary(
            visual_release_gate
        )
        final_response = render_final_response(
            response_run_status,
            scenario_id=scenario_id,
            skill_transcript_gate=skill_transcript_gate,
        )
        visual_release_gate = _visual_release_gate(
            run_status,
            final_response=final_response,
            scenario_id=scenario_id,
            check_final_response=True,
        )
    transcript = (
        "# Fresh Session Transcript\n\n"
        "SKILL INSTRUCTIONS LOADED:\n"
        f"- path: {skill_transcript_gate.get('skill_path')}\n"
        f"- sha256: {skill_transcript_gate.get('skill_sha256')}\n"
        f"- route command: {skill_transcript_gate.get('route_command')}\n"
        f"- status: {skill_transcript_gate.get('status')}\n\n"
        f"USER: {scenario['invocation']}\n\n"
        "ASSISTANT:\n"
        f"{final_response}"
    )
    transcript_path.write_text(transcript, encoding="utf-8")
    validation = validate_final_response(
        run_status=response_run_status,
        final_response=final_response,
        scenario_id=scenario_id,
        skill_transcript_gate=skill_transcript_gate,
    )
    expected = _expected_outcome_ok(scenario, run_status, validation)
    failures = list(validation["failures"])
    if not expected["ok"]:
        failures.append(_failure(scenario_id, expected["check"], expected["detail"]))
    if visual_release_gate is not None:
        expected_release = scenario.get("expected_release_gate_pass")
        if expected_release is True and visual_release_gate["release_gate_passed"] is not True:
            failures.extend(visual_release_gate.get("failures", []))
            failures.append(
                _failure(
                    scenario_id,
                    "visual_release_gate_not_passed",
                    "scenario was expected to satisfy completed_auto_visual release criteria",
                )
            )
        if expected_release is False and visual_release_gate["release_gate_passed"] is True:
            failures.append(
                _failure(
                    scenario_id,
                    "non_release_visual_scenario_counted",
                    "fixture, manual, user-provided, or blocked visual evidence counted as a release pass",
                )
            )

    summary = {
        "id": scenario_id,
        "description": scenario["description"],
        "status": "passed" if not failures else "failed",
        "expected_terminal_outcome": scenario["expected_terminal_outcome"],
        "terminal_outcome": validation["terminal_outcome"],
        "provenance_class": validation["provenance_class"],
        "selected_mode": run_status.get("selected_mode"),
        "run_status": run_status.get("status"),
        "ok": run_status.get("ok"),
        "terminal": run_status.get("terminal"),
        "run_dir": run_status.get("run_dir"),
        "artifacts": dict(run_status.get("artifacts", {}))
        if isinstance(run_status.get("artifacts"), Mapping)
        else {},
        "shard_summary": dict(run_status.get("shard_summary", {}))
        if isinstance(run_status.get("shard_summary"), Mapping)
        else {},
        "fallback": dict(run_status.get("fallback", {}))
        if isinstance(run_status.get("fallback"), Mapping)
        else {},
        "diagnostics": dict(run_status.get("diagnostics", {}))
        if isinstance(run_status.get("diagnostics"), Mapping)
        else {},
        "transcript": str(transcript_path.resolve()),
        "returncode": completed.returncode if completed is not None else None,
        "stderr": completed.stderr.strip()[:500] if completed is not None else "",
        "timed_out": timed_out,
        "validation": validation,
        "failures": failures,
    }
    if visual_release_gate is not None:
        summary["visual_release_gate"] = visual_release_gate
    return summary


def _fixture_scenario(invocation: str) -> dict[str, Any]:
    return {
        "id": "fixture_full_runner",
        "description": "deterministic full-runner artifact handoff using fixture shards",
        "invocation": invocation,
        "expected_terminal_outcome": "completed_fixture",
        "command": [
            "invoke",
            invocation,
            "--runs-dir",
            "__RUNS_DIR__",
            "--adapter",
            "fixture",
            "--route",
            "text_only",
            "--budget",
            "quick",
            "--min-tasks",
            "3",
            "--max-tasks",
            "3",
        ],
    }


def _visual_fixture_scenario(invocation: str) -> dict[str, Any]:
    return {
        "id": "visual_fixture_not_release_pass",
        "description": (
            "deterministic visual-required fixture coverage that must not satisfy "
            "the real-use visual release gate"
        ),
        "invocation": invocation,
        "expected_terminal_outcome": "completed_fixture",
        "expected_release_gate_pass": False,
        "visual_release_gate": True,
        "command": [
            "invoke",
            invocation,
            "--runs-dir",
            "__RUNS_DIR__",
            "--adapter",
            "fixture",
            "--route",
            "visual_required",
            "--budget",
            "quick",
            "--min-tasks",
            "3",
            "--max-tasks",
            "3",
        ],
    }


def _visual_public_safe_provider_blocked_scenario(invocation: str) -> dict[str, Any]:
    return {
        "id": "visual_required_provider_blocked",
        "description": (
            "visual-required fresh-session prompt records missing real visual provider "
            "without counting as a release-gate pass"
        ),
        "invocation": invocation,
        "expected_terminal_outcome": "blocked_explicit",
        "expected_release_gate_pass": False,
        "visual_release_gate": True,
        "command": [
            "invoke",
            invocation,
            "--runs-dir",
            "__RUNS_DIR__",
            "--adapter",
            "serial-degraded",
            "--route",
            "visual_required",
            "--budget",
            "quick",
            "--min-tasks",
            "1",
            "--max-tasks",
            "1",
            "--no-degrade",
        ],
    }


def _visual_completed_auto_visual_scenario(
    run_status: Mapping[str, Any],
    *,
    invocation: str,
) -> dict[str, Any]:
    return {
        "id": "visual_completed_auto_release_candidate",
        "description": (
            "operator-supplied completed_auto_visual artifact handoff must satisfy "
            "the real-use visual release gate"
        ),
        "invocation": str(run_status.get("invocation") or invocation),
        "expected_terminal_outcome": "completed_auto_visual",
        "expected_release_gate_pass": True,
        "visual_release_gate": True,
        "run_status": dict(run_status),
    }


def _serial_fallback_scenario(invocation: str) -> dict[str, Any]:
    return {
        "id": "serial_fallback_blocked",
        "description": "serial fallback path records blocked terminal status without fake shards",
        "invocation": invocation,
        "expected_terminal_outcome": "blocked_explicit",
        "command": [
            "invoke",
            invocation,
            "--runs-dir",
            "__RUNS_DIR__",
            "--adapter",
            "serial-degraded",
            "--route",
            "text_only",
            "--budget",
            "quick",
            "--min-tasks",
            "1",
            "--max-tasks",
            "1",
        ],
    }


def _real_codex_exec_scenario(invocation: str, *, real_codex_exec: str) -> dict[str, Any]:
    codex_path = shutil.which("codex")
    if real_codex_exec == "skip":
        return _skipped_real_codex_scenario(
            invocation,
            detail="real codex-exec scenario skipped by --real-codex-exec=skip",
            codex_available=bool(codex_path),
        )
    if real_codex_exec == "auto" and not codex_path:
        return {
            "id": "real_codex_exec_blocked",
            "description": "real codex-exec unavailable preflight is explicit",
            "invocation": invocation,
            "expected_terminal_outcome": "blocked_explicit",
            "command": [
                "invoke",
                invocation,
                "--runs-dir",
                "__RUNS_DIR__",
                "--adapter",
                "codex-exec",
                "--route",
                "text_only",
                "--budget",
                "quick",
                "--min-tasks",
                "1",
                "--max-tasks",
                "1",
                "--no-degrade",
                "--require-codex-exec",
            ],
        }
    return {
        "id": "real_codex_exec",
        "description": "real codex-exec child execution is asserted when available",
        "invocation": invocation,
        "expected_terminal_outcome": (
            "completed_real_parallel"
            if real_codex_exec == "require"
            else "real_parallel_or_blocked_explicit"
        ),
        "command": [
            "invoke",
            invocation,
            "--runs-dir",
            "__RUNS_DIR__",
            "--adapter",
            "codex-exec",
            "--route",
            "text_only",
            "--budget",
            "quick",
            "--min-tasks",
            "1",
            "--max-tasks",
            "1",
            "--no-degrade",
            "--require-codex-exec",
        ],
    }


def _skipped_real_codex_scenario(
    invocation: str, *, detail: str, codex_available: bool
) -> dict[str, Any]:
    run_status = {
        "schema_version": "codex-deepresearch.run-status.v0",
        "run_id": None,
        "run_dir": None,
        "invocation": invocation,
        "question": invocation.split(":", 1)[1].strip() if ":" in invocation else invocation,
        "selected_mode": "blocked",
        "status": "blocked_preflight",
        "ok": False,
        "terminal": True,
        "provenance": {
            "type": "blocked_preflight",
            "adapter": "codex-exec",
            "fixture_only": False,
            "manual_handoff": False,
            "attempted_real_child_execution": False,
            "real_child_execution": False,
            "real_use_e2e_eligible": False,
        },
        "diagnostics": {
            "actionable_cause": detail,
            "codex_cli_available": codex_available,
            "retry": "rerun with --real-codex-exec=auto or require to launch real child runs",
        },
        "artifacts": {},
        "shard_summary": {
            "planned_task_count": None,
            "accepted_shard_count": 0,
            "merged_shard_count": 0,
            "failed_task_count": 0,
            "blocked_task_count": 0,
            "rejected_shard_count": 0,
            "discarded_task_count": 0,
        },
        "fallback": {
            "parallel_degraded": None,
            "needs_serial_handoff": None,
            "degraded_reason": None,
        },
    }
    return {
        "id": "real_codex_exec_skipped",
        "description": "real codex-exec launch was skipped with an explicit blocked diagnostic",
        "invocation": invocation,
        "expected_terminal_outcome": "blocked_explicit",
        "run_status": run_status,
    }


def _visual_release_gate(
    run_status: Mapping[str, Any],
    *,
    final_response: str,
    scenario_id: str,
    check_final_response: bool,
) -> dict[str, Any]:
    status = str(run_status.get("status") or "")
    run_dir = _run_dir_from_status(run_status)
    artifacts = run_status.get("artifacts") if isinstance(run_status.get("artifacts"), Mapping) else {}
    counts = _visual_evidence_counts(run_dir)
    visual_artifact_validation = {"valid": False, "errors": []}
    if status == "completed_auto_visual" and run_dir is not None:
        validation = validate_visual_artifacts(run_dir=run_dir)
        visual_artifact_validation = validation.to_dict()

    artifact_exposure = _visual_artifact_exposure(
        status=status,
        run_dir=run_dir,
        artifacts=artifacts,
        final_response=final_response,
        check_final_response=check_final_response,
    )
    checks = {
        "status_completed_auto_visual": status == "completed_auto_visual",
        "run_ok_terminal": run_status.get("ok") is True and run_status.get("terminal") is True,
        "visual_artifacts_schema_valid": visual_artifact_validation.get("valid") is True,
        "visual_provider_status_completed_auto_visual": (
            counts["visual_provider_status_completed_auto_visual"]
        ),
        "visual_provider_minimums_satisfied": counts[
            "visual_provider_minimums_satisfied"
        ],
        "real_automatic_visual_candidates_at_least_10": (
            counts["real_candidate_count"] >= DEFAULT_MIN_REAL_VISUAL_CANDIDATES
        ),
        "real_fetched_artifacts_at_least_3": counts["real_fetched_artifacts"] >= 3,
        "report_status_completed": counts["report_status_completed"],
        "codex_interactive_analyzed_non_fixture_images_at_least_3": (
            counts["codex_interactive_analyzed_images"] >= 3
        ),
        "report_cited_visual_or_mixed_claim_at_least_1": (
            counts["report_cited_visual_or_mixed_claims"] >= 1
        ),
        "fixture_manual_user_evidence_excluded": counts["release_evidence_is_non_fixture"],
        "final_transcript_exposes_required_artifacts": artifact_exposure["ok"],
        "final_transcript_exposes_status_summary": (
            not check_final_response or "Visual summary:" in final_response
        ),
    }
    release_gate_passed = all(checks.values())
    failures = [
        _failure(scenario_id, check, "visual release-gate check did not pass")
        for check, passed in checks.items()
        if passed is not True and status == "completed_auto_visual"
    ]
    failures.extend(artifact_exposure["failures"])
    return {
        "schema_version": FRESH_SESSION_VISUAL_E2E_SCHEMA_VERSION,
        "status": status,
        "release_gate_passed": release_gate_passed,
        "blocked_capability": _blocked_capability(run_status),
        "blocked_detail": _blocked_detail(run_status),
        "real_candidate_count": counts["real_candidate_count"],
        "real_fetched_artifacts": counts["real_fetched_artifacts"],
        "codex_interactive_analyzed_images": counts["codex_interactive_analyzed_images"],
        "report_cited_visual_or_mixed_claims": counts[
            "report_cited_visual_or_mixed_claims"
        ],
        "non_release_visual_images": counts["non_release_visual_images"],
        "visual_minimums": counts["visual_minimums"],
        "real_automatic_visual_counts": counts["real_automatic_visual_counts"],
        "visual_artifact_validation": visual_artifact_validation,
        "required_response_artifacts": artifact_exposure["required_artifacts"],
        "checks": checks,
        "failures": failures,
    }


def _visual_release_summary(visual_release_gate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": visual_release_gate.get("status"),
        "release_gate_passed": visual_release_gate.get("release_gate_passed"),
        "blocked_capability": visual_release_gate.get("blocked_capability"),
        "real_candidate_count": visual_release_gate.get("real_candidate_count", 0),
        "real_fetched_artifacts": visual_release_gate.get("real_fetched_artifacts", 0),
        "codex_interactive_analyzed_images": visual_release_gate.get(
            "codex_interactive_analyzed_images", 0
        ),
        "report_cited_visual_or_mixed_claims": visual_release_gate.get(
            "report_cited_visual_or_mixed_claims", 0
        ),
    }


def _visual_artifact_exposure(
    *,
    status: str,
    run_dir: Path | None,
    artifacts: Mapping[str, Any],
    final_response: str,
    check_final_response: bool,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    required = _required_visual_artifacts(status)
    if run_dir is None:
        return {
            "ok": status in _EXPLICIT_TERMINAL_STATUSES,
            "required_artifacts": [],
            "failures": failures,
        }
    exposed = True
    for key in required:
        value = artifacts.get(key)
        if not isinstance(value, str) or not value.strip():
            value = str(run_dir / _visual_artifact_filename(key))
        artifact_path = Path(value) if Path(value).is_absolute() else run_dir / value
        if not artifact_path.is_file():
            exposed = False
            failures.append(
                _failure(
                    "visual",
                    f"missing_{key}_artifact_file",
                    f"required visual artifact file does not exist: {artifact_path}",
                )
            )
            continue
        if check_final_response and str(artifact_path) not in final_response and value not in final_response:
            exposed = False
            failures.append(
                _failure(
                    "visual",
                    f"missing_{key}_artifact_in_response",
                    f"final response does not expose {key} artifact path",
                )
            )
    if check_final_response and "Visual summary:" not in final_response:
        exposed = False
    return {"ok": exposed, "required_artifacts": required, "failures": failures}


def _required_visual_artifacts(status: str) -> list[str]:
    required = [
        "run_status",
        "evidence",
        "visual_tasks",
        "visual_observations",
        "visual_provider_status",
    ]
    if status in _SYNTHESIZED_SUCCESS_STATUSES:
        required.extend(["report", "report_status"])
    if status == "completed_auto_visual":
        required.extend(["visual_candidates", "image_fetch_status"])
    return required


def _visual_artifact_filename(key: str) -> str:
    return {
        "run_status": "run_status.json",
        "evidence": "evidence.json",
        "visual_tasks": "visual_tasks.json",
        "visual_observations": "visual_observations.jsonl",
        "visual_provider_status": VISUAL_PROVIDER_STATUS_FILENAME,
        "report": "report.md",
        "report_status": "report_status.json",
        "visual_candidates": VISUAL_CANDIDATES_FILENAME,
        "image_fetch_status": IMAGE_FETCH_STATUS_FILENAME,
    }.get(key, key)


def _visual_evidence_counts(run_dir: Path | None) -> dict[str, Any]:
    if run_dir is None:
        return _empty_visual_counts()
    evidence = _read_optional_json(run_dir / "evidence.json")
    report_status = _read_optional_json(run_dir / "report_status.json")
    candidates = _read_optional_jsonl(run_dir / VISUAL_CANDIDATES_FILENAME)
    fetches = _read_optional_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
    observations = _read_optional_jsonl(run_dir / "visual_observations.jsonl")
    visual_provider_status = _read_optional_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME)
    report_text = _read_optional_text(run_dir / "report.md")
    images = _mapping_list(evidence.get("images") if isinstance(evidence, Mapping) else [])
    visual_minimums = visual_minimums_for_run(run_dir)
    provider_minimums = (
        visual_provider_status.get("minimums")
        if isinstance(visual_provider_status, Mapping)
        and isinstance(visual_provider_status.get("minimums"), Mapping)
        else {}
    )

    codex_image_ids = _codex_interactive_real_evidence_image_ids(
        images=images,
        observations=observations,
    )
    non_release_images = sum(
        1
        for image in images
        if str(image.get("provider_mode") or "") in {"fixture", "manual", "user_provided"}
    )
    report_cited_claims = _report_cited_visual_or_mixed_claims(
        evidence if isinstance(evidence, Mapping) else {},
        report_status if isinstance(report_status, Mapping) else {},
        eligible_image_ids=codex_image_ids,
        report_text=report_text,
    )
    return {
        "codex_interactive_analyzed_images": len(codex_image_ids),
        "report_cited_visual_or_mixed_claims": report_cited_claims,
        "non_release_visual_images": non_release_images,
        "release_evidence_is_non_fixture": non_release_images == 0,
        "real_candidate_count": int(visual_minimums.get("candidate_count") or 0),
        "real_fetched_artifacts": int(visual_minimums.get("fetched_artifacts") or 0),
        "visual_minimums": visual_minimums,
        "visual_provider_minimums_satisfied": (
            visual_minimums.get("satisfied") is True
            and provider_minimums.get("satisfied") is True
            and int(provider_minimums.get("candidate_count") or 0)
            >= DEFAULT_MIN_REAL_VISUAL_CANDIDATES
            and int(provider_minimums.get("fetched_artifacts") or 0) >= 3
            and int(provider_minimums.get("vlm_images_analyzed") or 0) >= 3
            and int(provider_minimums.get("report_cited_images") or 0) >= 1
        ),
        "visual_provider_status_completed_auto_visual": (
            isinstance(visual_provider_status, Mapping)
            and visual_provider_status.get("status") == "completed_auto_visual"
        ),
        "report_status_completed": (
            isinstance(report_status, Mapping) and report_status.get("status") == "completed"
        ),
        "real_automatic_visual_counts": real_automatic_visual_release_counts(
            candidates=candidates,
            fetches=fetches,
            observations=observations,
            visual_provider_status=visual_provider_status
            if isinstance(visual_provider_status, Mapping)
            else None,
        ),
    }


def _empty_visual_counts() -> dict[str, Any]:
    return {
        "codex_interactive_analyzed_images": 0,
        "report_cited_visual_or_mixed_claims": 0,
        "non_release_visual_images": 0,
        "release_evidence_is_non_fixture": True,
        "real_candidate_count": 0,
        "real_fetched_artifacts": 0,
        "visual_minimums": {
            "required_vlm_images": 3,
            "candidate_count": 0,
            "selected_candidates": 0,
            "fetched_artifacts": 0,
            "vlm_images_analyzed": 0,
            "report_cited_images": 0,
            "satisfied": False,
            "shortfall_reason": "insufficient_candidates",
        },
        "visual_provider_minimums_satisfied": False,
        "visual_provider_status_completed_auto_visual": False,
        "report_status_completed": False,
        "real_automatic_visual_counts": real_automatic_visual_release_counts(),
    }


def _is_codex_interactive_real_analyzed(record: Mapping[str, Any]) -> bool:
    if record.get("provider_mode") != "real":
        return False
    provider_values = {
        str(record.get(key) or "")
        for key in (
            "provider",
            "visual_provider",
            "analysis_provider",
            "model_or_tool",
            "vlm_provider",
        )
    }
    if "codex-interactive" not in provider_values:
        return False
    status = str(record.get("observation_status") or record.get("analysis_status") or "")
    return status == "analyzed"


def _codex_interactive_real_evidence_image_ids(
    *,
    images: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> set[str]:
    images_by_id = {
        image.get("id"): image
        for image in images
        if isinstance(image.get("id"), str) and image.get("id")
    }
    image_ids: set[str] = set()
    for observation in observations:
        if not _is_codex_interactive_real_analyzed(observation):
            continue
        image_id = observation.get("evidence_image_id")
        if not isinstance(image_id, str) or not image_id:
            continue
        image = images_by_id.get(image_id)
        if image is None:
            continue
        if not _is_codex_interactive_real_evidence_image(image):
            continue
        if not _evidence_image_matches_observation(image, observation):
            continue
        image_ids.add(image_id)
    return image_ids


def _is_codex_interactive_real_evidence_image(image: Mapping[str, Any]) -> bool:
    if image.get("provider_mode") != "real":
        return False
    if image.get("analysis_status") != "analyzed":
        return False
    if str(image.get("policy_decision") or "allowed") != "allowed":
        return False
    if image.get("policy_flags"):
        return False
    provider_values = {
        str(image.get(key) or "")
        for key in (
            "analysis_provider",
            "visual_provider",
            "model_or_tool",
            "vlm_provider",
        )
    }
    if "codex-interactive" not in provider_values:
        return False
    return bool(_string_items(image.get("observations")))


def _evidence_image_matches_observation(
    image: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> bool:
    for key in ("candidate_id", "fetch_id", "task_id", "angle_id"):
        image_value = image.get(key)
        observation_value = observation.get(key)
        if (
            isinstance(image_value, str)
            and image_value
            and isinstance(observation_value, str)
            and observation_value
            and image_value != observation_value
        ):
            return False
    image_observations = set(_string_items(image.get("observations")))
    observed_text = set(_string_items(observation.get("observations")))
    if (
        image_observations
        and observed_text
        and not image_observations.intersection(observed_text)
    ):
        return False
    return True


def _report_cited_visual_or_mixed_claims(
    evidence: Mapping[str, Any],
    report_status: Mapping[str, Any],
    *,
    eligible_image_ids: set[str],
    report_text: str,
) -> int:
    used_images = {
        image_id
        for image_id in report_status.get("used_images", [])
        if isinstance(image_id, str) and image_id
    }
    used_images &= eligible_image_ids
    if not used_images:
        return 0
    included_claims = _mapping_list(report_status.get("included_claims", []))
    included_ids = {
        claim.get("claim_id")
        for claim in included_claims
        if isinstance(claim.get("claim_id"), str)
    }
    included_images_by_claim = {
        str(claim.get("claim_id")): {
            image_id
            for image_id in claim.get("image_ids", [])
            if isinstance(image_id, str) and image_id in used_images
        }
        for claim in included_claims
        if isinstance(claim.get("claim_id"), str)
    }
    claims = _mapping_list(evidence.get("claims", []))
    cited_count = 0
    for claim in claims:
        claim_id = claim.get("id")
        if not isinstance(claim_id, str) or not claim_id:
            continue
        if claim.get("claim_type") not in {"visual", "mixed"}:
            continue
        if claim.get("verification_status") != "supported":
            continue
        if included_ids and claim_id not in included_ids:
            continue
        linked_images = used_images.intersection(_string_items(claim.get("supporting_images")))
        if included_images_by_claim:
            linked_images &= included_images_by_claim.get(claim_id, set())
        linked_images = {
            image_id
            for image_id in linked_images
            if _claim_visual_supports_image(claim, image_id)
        }
        if not linked_images:
            continue
        if not _report_markdown_cites_visual_claim(
            report_text,
            claim=claim,
            image_ids=linked_images,
        ):
            continue
        cited_count += 1
    return cited_count


def _claim_visual_supports_image(claim: Mapping[str, Any], image_id: str) -> bool:
    for support in _mapping_list(claim.get("visual_supports", [])):
        if support.get("image_id") == image_id and isinstance(
            support.get("observation_ref"), str
        ):
            return True
    return False


def _report_markdown_cites_visual_claim(
    report_text: str,
    *,
    claim: Mapping[str, Any],
    image_ids: set[str],
) -> bool:
    if not report_text.strip():
        return False
    claim_id = claim.get("id")
    claim_text = claim.get("text")
    has_claim = any(
        isinstance(marker, str) and marker and marker in report_text
        for marker in (claim_id, claim_text)
    )
    if not has_claim:
        return False
    return any(
        image_id in report_text or f"img:{image_id}" in report_text
        for image_id in image_ids
    )


def _blocked_capability(run_status: Mapping[str, Any]) -> str | None:
    diagnostics = (
        run_status.get("diagnostics") if isinstance(run_status.get("diagnostics"), Mapping) else {}
    )
    required = diagnostics.get("required_capability")
    if isinstance(required, str) and required:
        return required
    status = str(run_status.get("status") or "")
    if status == "blocked_missing_visual_provider":
        return "visual_provider"
    if status == "blocked_missing_vlm_provider":
        return "vlm_provider"
    if status == "blocked_missing_search_handoff":
        return "search_handoff"
    actionable = str(diagnostics.get("actionable_cause") or "").lower()
    if "codex exec" in actionable:
        return "codex-exec"
    if "visual" in actionable and "provider" in actionable:
        return "visual_provider"
    if "vlm" in actionable:
        return "vlm_provider"
    if "search" in actionable or "handoff" in actionable:
        return "search_handoff"
    return None


def _blocked_detail(run_status: Mapping[str, Any]) -> str | None:
    diagnostics = (
        run_status.get("diagnostics") if isinstance(run_status.get("diagnostics"), Mapping) else {}
    )
    detail = diagnostics.get("actionable_cause")
    return detail if isinstance(detail, str) and detail else None


def _load_completed_auto_visual_run_status(path: Path) -> dict[str, Any]:
    run_status_path = path if path.name == "run_status.json" else path / "run_status.json"
    if not run_status_path.is_file():
        raise FreshSessionE2EError(
            "completed_auto_visual run must point to a run directory or run_status.json: "
            f"{path}"
        )
    payload = json.loads(run_status_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise FreshSessionE2EError(f"run_status.json is not a JSON object: {run_status_path}")
    return dict(payload)


def _payload_from_subprocess(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    raw = completed.stdout.strip() or completed.stderr.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        return {
            "schema_version": "codex-deepresearch.run-status.v0",
            "run_id": None,
            "run_dir": None,
            "selected_mode": "blocked",
            "status": "blocked_preflight",
            "ok": False,
            "terminal": True,
            "provenance": {"type": "blocked_preflight"},
            "diagnostics": {
                "actionable_cause": f"runner did not emit JSON: {exc}",
                "stdout": completed.stdout[:500],
                "stderr": completed.stderr[:500],
                "returncode": completed.returncode,
            },
            "artifacts": {},
        }


def _timeout_run_status(
    *,
    scenario: Mapping[str, Any],
    command: Sequence[str],
    timeout_seconds: float,
    exc: subprocess.TimeoutExpired,
) -> dict[str, Any]:
    invocation = str(scenario.get("invocation") or "")
    return {
        "schema_version": "codex-deepresearch.run-status.v0",
        "run_id": None,
        "run_dir": None,
        "invocation": invocation,
        "question": invocation.split(":", 1)[1].strip() if ":" in invocation else invocation,
        "selected_mode": "blocked",
        "status": "blocked_preflight",
        "ok": False,
        "terminal": True,
        "provenance": {
            "type": "blocked_preflight",
            "adapter": _adapter_from_command(command),
            "fixture_only": False,
            "manual_handoff": False,
            "attempted_real_child_execution": _adapter_from_command(command) == "codex-exec",
            "real_child_execution": False,
            "real_use_e2e_eligible": False,
        },
        "diagnostics": {
            "actionable_cause": (
                f"fresh-session scenario timed out after {timeout_seconds:g} seconds"
            ),
            "timeout_seconds": timeout_seconds,
            "command": _redacted_command(command),
            "stdout": _timeout_text(getattr(exc, "stdout", None)),
            "stderr": _timeout_text(getattr(exc, "stderr", None)),
        },
        "artifacts": {},
        "shard_summary": {
            "planned_task_count": None,
            "accepted_shard_count": 0,
            "merged_shard_count": 0,
            "failed_task_count": 0,
            "blocked_task_count": 0,
            "rejected_shard_count": 0,
            "discarded_task_count": 0,
        },
        "fallback": {
            "parallel_degraded": None,
            "needs_serial_handoff": None,
            "degraded_reason": "scenario_timeout",
        },
    }


def _expected_outcome_ok(
    scenario: Mapping[str, Any],
    run_status: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> dict[str, Any]:
    expected = str(scenario["expected_terminal_outcome"])
    terminal = str(validation["terminal_outcome"])
    if expected == terminal:
        return {"ok": True, "check": "expected_terminal_outcome", "detail": ""}
    if expected == "real_parallel_or_blocked_explicit" and terminal in {
        "completed_real_parallel",
        "blocked_explicit",
    }:
        return {"ok": True, "check": "expected_terminal_outcome", "detail": ""}
    status = str(run_status.get("status") or "")
    return {
        "ok": False,
        "check": "unexpected_terminal_outcome",
        "detail": f"expected {expected}, got {terminal} from status {status}",
    }


def _skill_transcript_gate(
    *,
    runner_path: Path,
    skill_path: Path,
    invocation: str,
) -> dict[str, Any]:
    manifest_path = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
    checks: dict[str, bool] = {
        "manifest_exists": manifest_path.is_file(),
        "canonical_skill_exists": skill_path.is_file(),
        "runner_executable": runner_path.is_file(),
        "normal_invocation": invocation.startswith("$deep-research:"),
    }
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    checks["manifest_name"] = manifest.get("name") == "codex-deepresearch"
    checks["manifest_skills"] = manifest.get("skills") == "./skills/"
    try:
        skill_text = skill_path.read_text(encoding="utf-8")
    except OSError:
        skill_text = ""
    route_command = _extract_route_command(skill_text)
    checks["skill_names_deep_research"] = "name: deep-research" in skill_text
    checks["skill_has_invocation_router_section"] = "## Invocation Router" in skill_text
    checks["skill_references_deep_research_invocation"] = "$deep-research:" in skill_text
    checks["skill_forbids_normal_chat_only"] = (
        "do not answer directly in chat" in skill_text.lower()
    )
    checks["skill_routes_normal_invocation_through_runner"] = bool(route_command)
    checks["skill_reserves_quick_chat_for_explicit_request"] = (
        "quick-chat only when the user explicitly asks" in skill_text.lower()
        or "quick-chat` only when the user explicitly asks" in skill_text.lower()
    )
    checks["skill_reports_blocked_status"] = (
        "blocked statuses must expose" in skill_text.lower()
        and "do not silently return a chat-only answer" in skill_text.lower()
    )
    passed = all(checks.values())
    return {
        "status": "passed" if passed else "failed",
        "checks": checks,
        "skill_path": str(skill_path.resolve()) if skill_path.exists() else str(skill_path),
        "skill_sha256": sha256(skill_text.encode("utf-8")).hexdigest() if skill_text else None,
        "route_command": route_command,
        "transcript_source": "canonical_skill_instructions",
        "detail": (
            "canonical skill instructions route normal invocations through the runner"
            if passed
            else "canonical skill instructions do not satisfy the fresh-session invocation contract"
        ),
    }


def _runner_artifact_gate(scenarios: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    skill_checks = {"skill_transcript_gate_not_passed", "not_skill_invocation_transcript"}
    failures = [
        failure
        for scenario in scenarios
        for failure in scenario.get("validation", {}).get("failures", [])
        if isinstance(failure, Mapping) and failure.get("check") not in skill_checks
    ]
    passed = not failures
    return {
        "status": "passed" if passed else "failed",
        "detail": (
            "runner artifact handoff validation passed for all scripted scenarios"
            if passed
            else "runner artifact handoff validation failed for one or more scripted scenarios"
        ),
        "failures": failures,
    }


def _acceptance(scenarios: Sequence[Mapping[str, Any]]) -> dict[str, bool]:
    by_id = {str(scenario.get("id")): scenario for scenario in scenarios}
    fixture = by_id.get("fixture_full_runner", {})
    serial = by_id.get("serial_fallback_blocked", {})
    real = next(
        (
            scenario
            for scenario in scenarios
            if str(scenario.get("id")).startswith("real_codex_exec")
        ),
        {},
    )
    scenario_statuses = [scenario.get("status") == "passed" for scenario in scenarios]
    return {
        "fresh_session_not_chat_only": all(
            scenario.get("selected_mode") != "quick-chat" for scenario in scenarios
        ),
        "required_artifact_files_exist": all(
            scenario.get("validation", {}).get("status") == "passed"
            for scenario in scenarios
        ),
        "real_codex_exec_asserted_or_explicit": real.get("terminal_outcome")
        in {"completed_real_parallel", "blocked_explicit"},
        "provenance_distinguishes_fixture_serial_real": _distinguishes_provenance(
            fixture.get("provenance_class"),
            serial.get("provenance_class"),
            real.get("provenance_class"),
        ),
        "ci_public_safe_without_private_artifacts": all(scenario_statuses),
    }


def _visual_acceptance(scenarios: Sequence[Mapping[str, Any]]) -> dict[str, bool]:
    visual_gates = [
        scenario.get("visual_release_gate")
        for scenario in scenarios
        if isinstance(scenario.get("visual_release_gate"), Mapping)
    ]
    blocked_gates = [
        scenario.get("visual_release_gate")
        for scenario in scenarios
        if scenario.get("terminal_outcome") == "blocked_explicit"
        and isinstance(scenario.get("visual_release_gate"), Mapping)
    ]
    fixture_gates = [
        scenario.get("visual_release_gate")
        for scenario in scenarios
        if scenario.get("provenance_class") == "fixture_only"
        and isinstance(scenario.get("visual_release_gate"), Mapping)
    ]
    release_gate_passed = any(gate.get("release_gate_passed") is True for gate in visual_gates)
    blocked_capabilities = {
        "codex-exec",
        "search_handoff",
        "visual_provider",
        "vlm_provider",
    }
    return {
        "visual_required_prompt_exercised": bool(visual_gates),
        "release_gate_passed": release_gate_passed,
        "completed_auto_visual_validation_rules_met": all(
            gate.get("release_gate_passed") is True
            for gate in visual_gates
            if gate.get("status") == "completed_auto_visual"
        ),
        "blocked_runs_name_missing_capability": all(
            gate.get("blocked_capability") in blocked_capabilities for gate in blocked_gates
        ),
        "blocked_runs_do_not_count_as_release_passes": all(
            gate.get("release_gate_passed") is not True for gate in blocked_gates
        ),
        "fixture_manual_user_evidence_excluded": all(
            gate.get("release_gate_passed") is not True for gate in fixture_gates
        ),
        "final_transcript_exposes_artifacts_and_status_summary": all(
            gate.get("checks", {}).get("final_transcript_exposes_required_artifacts") is True
            and gate.get("checks", {}).get("final_transcript_exposes_status_summary") is True
            for gate in visual_gates
        ),
    }


def _visual_release_gate_status(
    scenarios: Sequence[Mapping[str, Any]],
    *,
    real_codex_interactive: str,
) -> str:
    gates = [
        scenario.get("visual_release_gate")
        for scenario in scenarios
        if isinstance(scenario.get("visual_release_gate"), Mapping)
    ]
    if any(gate.get("release_gate_passed") is True for gate in gates):
        return "passed"
    if any(scenario.get("terminal_outcome") == "blocked_explicit" for scenario in scenarios):
        return "blocked_public_safe"
    if real_codex_interactive == "skip":
        return "not_run_public_safe"
    return "not_passed"


def _visual_outcome_counts(scenarios: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {
        "release_gate_passed": 0,
        "blocked_public_safe": 0,
        "fixture_not_release_pass": 0,
        "completed_auto_visual": 0,
    }
    for scenario in scenarios:
        gate = scenario.get("visual_release_gate")
        if not isinstance(gate, Mapping):
            continue
        if gate.get("release_gate_passed") is True:
            counts["release_gate_passed"] += 1
        if scenario.get("terminal_outcome") == "blocked_explicit":
            counts["blocked_public_safe"] += 1
        if scenario.get("provenance_class") == "fixture_only" and gate.get("release_gate_passed") is not True:
            counts["fixture_not_release_pass"] += 1
        if gate.get("status") == "completed_auto_visual":
            counts["completed_auto_visual"] += 1
    return counts


def _outcome_counts(scenarios: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {
        "completed_fixture": 0,
        "completed_real_parallel": 0,
        "blocked_explicit": 0,
        "serial_fallback": 0,
        "fixture_only": 0,
        "real_parallel": 0,
    }
    for scenario in scenarios:
        terminal = scenario.get("terminal_outcome")
        provenance = scenario.get("provenance_class")
        if terminal in counts:
            counts[str(terminal)] += 1
        if provenance in counts:
            counts[str(provenance)] += 1
    return counts


def _distinguishes_provenance(*classes: Any) -> bool:
    observed = {str(value) for value in classes if value}
    return {"fixture_only", "serial_fallback"} <= observed and bool(
        observed & {"real_parallel", "blocked"}
    )


def _terminal_outcome(run_status: Mapping[str, Any]) -> str:
    status = str(run_status.get("status") or "")
    provenance_class = _provenance_class(run_status)
    if status == "completed_auto_visual":
        return "completed_auto_visual"
    if status == "partial_auto_visual":
        return "partial_auto_visual"
    if provenance_class == "real_parallel" and status in {
        "completed_parallel",
        "completed_partial_parallel",
    }:
        return "completed_real_parallel"
    if provenance_class == "fixture_only" and status == "completed_fixture":
        return "completed_fixture"
    if status in _EXPLICIT_TERMINAL_STATUSES:
        return "blocked_explicit"
    if status in _SYNTHESIZED_SUCCESS_STATUSES:
        return "completed_full_runner"
    return "unknown"


def _provenance_class(run_status: Mapping[str, Any]) -> str:
    provenance = (
        run_status.get("provenance") if isinstance(run_status.get("provenance"), Mapping) else {}
    )
    source_type = str(provenance.get("type") or "")
    if provenance.get("fixture_only") is True or source_type == "fixture":
        return "fixture_only"
    if source_type == "serial_handoff" or provenance.get("adapter") == "serial-degraded":
        return "serial_fallback"
    if provenance.get("real_child_execution") is True or source_type == "real_child_execution":
        return "real_parallel"
    if source_type.startswith("blocked") or str(run_status.get("status") or "").startswith("blocked"):
        return "blocked"
    if str(run_status.get("status") or "").startswith("failed"):
        return "blocked"
    return source_type or "unknown"


def _required_artifacts(run_status: Mapping[str, Any]) -> list[str]:
    if _is_synthesized_success(run_status):
        return ["report", "evidence", "run_status", "report_status"]
    if run_status.get("run_dir"):
        return ["run_status"]
    return []


def _is_synthesized_success(run_status: Mapping[str, Any]) -> bool:
    if run_status.get("ok") is not True or run_status.get("terminal") is not True:
        return False
    if str(run_status.get("status") or "") not in _SYNTHESIZED_SUCCESS_STATUSES:
        return False
    artifacts = run_status.get("artifacts")
    return isinstance(artifacts, Mapping) and "report" in artifacts


def _require_artifact(
    failures: list[dict[str, Any]],
    *,
    scenario_id: str,
    artifact_key: str,
    artifacts: Mapping[str, Any],
    run_dir: Path | None,
    final_response: str,
) -> None:
    value = artifacts.get(artifact_key)
    if not isinstance(value, str) or not value.strip():
        failures.append(
            _failure(
                scenario_id,
                f"missing_{artifact_key}_artifact_path",
                f"missing required artifact path for {artifact_key}",
            )
        )
        return
    artifact_path = Path(value) if Path(value).is_absolute() else (run_dir / value if run_dir else Path(value))
    if not artifact_path.is_file():
        failures.append(
            _failure(
                scenario_id,
                f"missing_{artifact_key}_artifact_file",
                f"required artifact file does not exist: {artifact_path}",
            )
        )
    if str(artifact_path) not in final_response and value not in final_response:
        failures.append(
            _failure(
                scenario_id,
                f"missing_{artifact_key}_artifact_in_response",
                f"final response does not expose {artifact_key} artifact path",
            )
        )


def _looks_successful(final_response: str) -> bool:
    lowered = final_response.lower()
    return any(marker in lowered for marker in _SUCCESS_MARKERS)


def _failure(scenario_id: str, check: str, detail: str) -> dict[str, Any]:
    return {"scenario_id": scenario_id, "check": check, "detail": detail}


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _run_dir_from_status(run_status: Mapping[str, Any]) -> Path | None:
    value = run_status.get("run_dir")
    if not isinstance(value, str) or not value:
        return None
    return Path(value)


def _read_optional_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _read_optional_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _read_optional_jsonl(path: Path) -> list[Mapping[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    records: list[Mapping[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, Mapping):
            records.append(record)
    return records


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _string_items(value: Any) -> list[str]:
    return (
        [item for item in value if isinstance(item, str) and item]
        if isinstance(value, list)
        else []
    )


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _extract_route_command(skill_text: str) -> str | None:
    for raw_line in skill_text.splitlines():
        line = raw_line.strip()
        if "codex-deepresearch" not in line or " invoke " not in f" {line} ":
            continue
        if "$deep-research:" not in line:
            continue
        if "plugins/codex-deepresearch/scripts/codex-deepresearch" not in line:
            continue
        return line
    return None


def _adapter_from_command(command: Sequence[str]) -> str | None:
    parts = [str(part) for part in command]
    for index, part in enumerate(parts):
        if part == "--adapter" and index + 1 < len(parts):
            return parts[index + 1]
    return None


def _redacted_command(command: Sequence[str]) -> list[str]:
    return [
        "<invocation>" if str(part).startswith("$deep-research:") else str(part)
        for part in command
    ]


def _timeout_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")[:500]
    return str(value)[:500]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _scenario_command_args(
    scenario: Mapping[str, Any],
    *,
    scenario_runs_dir: Path,
) -> list[str] | None:
    command = scenario.get("command")
    if command is None:
        return None
    return [
        str(scenario_runs_dir.resolve()) if part == "__RUNS_DIR__" else str(part)
        for part in command
    ]
