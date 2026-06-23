"""Execution mode and provider normalization for runner startup."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping


SCHEMA_VERSION = "codex-deepresearch.run-config.v0"

MODES = ("codex-plugin", "automated-cli", "manual-sources")

SEARCH_PROVIDERS_BY_MODE: Mapping[str, tuple[str, ...]] = {
    "codex-plugin": ("codex-native", "manual"),
    "automated-cli": ("openai", "manual", "brave", "tavily", "serpapi"),
    "manual-sources": ("manual",),
}

VLM_PROVIDERS_BY_MODE: Mapping[str, tuple[str, ...]] = {
    "codex-plugin": (
        "codex-interactive",
        "openai-responses-vision",
        "manual-visual-review",
    ),
    "automated-cli": ("openai-responses-vision", "manual-visual-review"),
    "manual-sources": (
        "manual-visual-review",
        "codex-interactive",
        "openai-responses-vision",
    ),
}

DEFAULT_SEARCH_PROVIDER_BY_MODE: Mapping[str, str] = {
    "codex-plugin": "codex-native",
    "automated-cli": "openai",
    "manual-sources": "manual",
}

DEFAULT_VLM_PROVIDER_BY_MODE: Mapping[str, str] = {
    "codex-plugin": "codex-interactive",
    "automated-cli": "openai-responses-vision",
    "manual-sources": "manual-visual-review",
}


@dataclass(frozen=True)
class BudgetPreset:
    max_codex_handoff_tasks: int
    max_concurrent_codex_subagents: int
    max_concurrent_runner_agents: int
    max_verifier_invocations: int
    max_model_api_calls: int
    max_sources: int
    max_images: int


BUDGET_PRESETS: Mapping[str, BudgetPreset] = {
    "quick": BudgetPreset(
        max_codex_handoff_tasks=16,
        max_concurrent_codex_subagents=4,
        max_concurrent_runner_agents=4,
        max_verifier_invocations=24,
        max_model_api_calls=32,
        max_sources=8,
        max_images=4,
    ),
    "standard": BudgetPreset(
        max_codex_handoff_tasks=48,
        max_concurrent_codex_subagents=8,
        max_concurrent_runner_agents=8,
        max_verifier_invocations=80,
        max_model_api_calls=96,
        max_sources=20,
        max_images=12,
    ),
    "deep": BudgetPreset(
        max_codex_handoff_tasks=96,
        max_concurrent_codex_subagents=24,
        max_concurrent_runner_agents=12,
        max_verifier_invocations=180,
        max_model_api_calls=220,
        max_sources=40,
        max_images=30,
    ),
    "exhaustive": BudgetPreset(
        max_codex_handoff_tasks=256,
        max_concurrent_codex_subagents=100,
        max_concurrent_runner_agents=16,
        max_verifier_invocations=500,
        max_model_api_calls=600,
        max_sources=100,
        max_images=80,
    ),
}


class ConfigResolutionError(ValueError):
    """Raised when runner startup configuration is invalid."""


@dataclass(frozen=True)
class RunConfig:
    schema_version: str
    mode: str
    search_provider: str
    vlm_provider: str
    budget_preset: str
    budget: BudgetPreset

    def to_dict(self) -> dict:
        data = asdict(self)
        data["budget"] = asdict(self.budget)
        return data


def _normalize_option(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower().replace("_", "-")
    if not normalized:
        raise ConfigResolutionError(f"{field_name} cannot be empty")
    return normalized


def _require_allowed(
    field_name: str,
    value: str,
    allowed_values: tuple[str, ...],
    *,
    mode: str | None = None,
) -> None:
    if value in allowed_values:
        return

    allowed = ", ".join(allowed_values)
    if mode:
        raise ConfigResolutionError(
            f"{field_name} '{value}' is not valid for mode '{mode}'; allowed: {allowed}"
        )
    raise ConfigResolutionError(f"{field_name} '{value}' is invalid; allowed: {allowed}")


def resolve_config(
    *,
    mode: str,
    search_provider: str | None = None,
    vlm_provider: str | None = None,
    budget_preset: str = "standard",
) -> RunConfig:
    """Normalize and validate execution-mode startup configuration."""

    normalized_mode = _normalize_option(mode, "mode")
    assert normalized_mode is not None
    _require_allowed("mode", normalized_mode, MODES)

    normalized_search_provider = _normalize_option(search_provider, "search_provider")
    if normalized_search_provider is None:
        normalized_search_provider = DEFAULT_SEARCH_PROVIDER_BY_MODE[normalized_mode]
    _require_allowed(
        "search provider",
        normalized_search_provider,
        SEARCH_PROVIDERS_BY_MODE[normalized_mode],
        mode=normalized_mode,
    )

    normalized_vlm_provider = _normalize_option(vlm_provider, "vlm_provider")
    if normalized_vlm_provider is None:
        normalized_vlm_provider = DEFAULT_VLM_PROVIDER_BY_MODE[normalized_mode]
    _require_allowed(
        "VLM provider",
        normalized_vlm_provider,
        VLM_PROVIDERS_BY_MODE[normalized_mode],
        mode=normalized_mode,
    )

    normalized_budget_preset = _normalize_option(budget_preset, "budget_preset")
    assert normalized_budget_preset is not None
    _require_allowed("budget preset", normalized_budget_preset, tuple(BUDGET_PRESETS))

    return RunConfig(
        schema_version=SCHEMA_VERSION,
        mode=normalized_mode,
        search_provider=normalized_search_provider,
        vlm_provider=normalized_vlm_provider,
        budget_preset=normalized_budget_preset,
        budget=BUDGET_PRESETS[normalized_budget_preset],
    )
