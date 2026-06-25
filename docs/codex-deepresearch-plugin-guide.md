# Codex DeepResearch Plugin Guide

This guide covers the Phase 3 local plugin install/update/remove flow, first-run quickstart, troubleshooting, and beta examples. Run commands from the repository root unless a command says otherwise.

Phase 3 has provider-gated paths. Fixture, local, manual, and user-provided evidence validate mechanics, but they are not release-eligible real provider runs. Automatic visual Public Beta runs require configured real visual acquisition plus an allowed VLM path, and blocked provider states should be reported honestly instead of converted into chat-only answers.

## Install

Prerequisites:

- Codex CLI is installed and `codex plugin --help` shows `add`, `list`, `marketplace`, and `remove`.
- This repository has the local marketplace metadata at `.agents/plugins/marketplace.json`.

Add the repository marketplace and install the plugin:

```bash
codex plugin marketplace add .
codex plugin list --marketplace codex-deepresearch-local --available
codex plugin add codex-deepresearch@codex-deepresearch-local
codex plugin list --json
```

`codex plugin list --json` should show `codex-deepresearch` as an installed plugin from `codex-deepresearch-local`. Start a new Codex thread after installation so the `$deep-research` skill is loaded. A normal invocation should select full-runner mode:

```text
$deep-research: Codex DeepResearch smoke test
```

The final response should name the selected mode and final status. If synthesis completes, it should include the run directory plus `report.md`, `evidence.json`, `run_status.json`, and `report_status.json`. If the run is blocked, it should expose `run_status.json` with `ok=false`, `terminal=true`, the exact `status`, and `diagnostics.actionable_cause`. It should not silently become a chat-only answer unless the prompt explicitly requested quick mode.

For a local CLI install/update smoke, run:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch smoke \
  --install \
  --invoke '$deep-research: Codex DeepResearch smoke test'
```

The smoke writes a timestamped run directory under `research-runs/` with `status.json`.

## Update Or Reinstall

For a local-path marketplace, pull or edit the repository, then reinstall the same marketplace plugin:

```bash
codex plugin add codex-deepresearch@codex-deepresearch-local
```

The local development path is `plugins/codex-deepresearch`; the local marketplace entry points at that directory, so edits under the plugin are picked up by reinstalling from `codex-deepresearch-local`. Start a new Codex thread after reinstalling. If the marketplace was added from a Git source rather than this local checkout, refresh the marketplace snapshot first:

```bash
codex plugin marketplace upgrade codex-deepresearch-local
codex plugin add codex-deepresearch@codex-deepresearch-local
```

Use `mvp-smoke` when you need the deterministic no-network validation suite:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch mvp-smoke \
  --runs-dir /tmp/codex-deepresearch-mvp-smoke \
  --suite-id mvp-smoke \
  --clean \
  --invoke '$deep-research: MVP smoke text-only fixture'
```

By default, `mvp-smoke` requires `codex` on `PATH` for the install/update capability check. If a CI environment cannot provide Codex CLI, use `--skip-codex-cli-install-check`; the result records the skip and does not mark plugin install/update as passed.

## Remove

Remove the installed plugin:

```bash
codex plugin remove codex-deepresearch@codex-deepresearch-local
```

Remove the local marketplace registration as well:

```bash
codex plugin marketplace remove codex-deepresearch-local
```

## Quickstart

Use full-runner mode by default:

```text
$deep-research: Compare the public evidence for two documented implementation options.
```

Use quick-chat only when the user explicitly asks for no evidence bundle:

```text
$deep-research: quick answer only, do not run the full pipeline: what is this repo for?
```

Quick-chat does not create a DeepResearch evidence bundle, and the assistant should say so.

Use manual handoff when the user provides sources directly:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch invoke \
  '$deep-research: summarize the supplied source' \
  --manual-handoff \
  --url https://example.com/source
```

Manual handoff records source metadata in a run directory without external search. Image URLs and local image files are recorded as visual evidence metadata. Further verification and synthesis still depend on the available local artifacts, guardrails, and review state.

## Artifact Handoff

`codex-plugin` mode does not assume hidden Codex search or VLM APIs. The handoff is artifact-based:

1. `prepare` creates a run directory with `search_tasks.json`, `search_results.jsonl`, `visual_tasks.json` when needed, `visual_observations.jsonl`, `budget_estimate.json`, and `run_status.json`.
2. The active Codex-side agent performs allowed search or visual review and writes handoff records.
3. `ingest` normalizes `search_results.jsonl` into `evidence.json` and `fetch_queue.json`.
4. Visual routes use `acquire-visual` and/or explicit `visual_observations.jsonl`, then `ingest-vision`.
5. `fetch-claims`, `enforce-guardrails`, `verify-claims`, and `synthesize` produce reviewed evidence and `report.md`.

Common inspection commands:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch run-status --run <run_id_or_path>
plugins/codex-deepresearch/scripts/codex-deepresearch monitor-detail --run <run_id_or_path>
plugins/codex-deepresearch/scripts/codex-deepresearch browse-evidence --run <run_id_or_path>
```

Important run artifacts:

- `run_status.json`: final or current status, terminal flag, blocker diagnostics, and status family.
- `report.md`: synthesized report from supported, reviewed claims only.
- `report_status.json`: synthesis manifest, used source/image IDs, and excluded evidence.
- `evidence.json`: schema-versioned sources, claims, images, votes, review state, and provenance.
- `parallel_orchestration_status.json`, `merge_status.json`, `subagent_assignments.jsonl`: parallel shard state when parallel orchestration ran.
- `visual_provider_status.json`, `visual_candidates.jsonl`, `image_fetch_status.jsonl`, `visual_observations.jsonl`: visual acquisition and VLM handoff state when visual work was attempted.
- `budget_estimate.json`: caps, pruning decisions, and budget confirmation requirements.

## Troubleshooting

`blocked_missing_search_handoff`: `prepare` created search tasks, but `search_results.jsonl` is missing, empty, invalid, or policy-blocked. Fill one valid `SearchResult` JSON object per line, use manual handoff for user-provided sources, or report the blocked status. Do not synthesize from search snippets alone.

`blocked_missing_visual_provider`: the selected route requires automatic visual evidence, but no real visual acquisition provider is configured or available. Local fixtures can validate mechanics, but they cannot satisfy Public Beta automatic visual release gates. Use a text-only route when visual evidence is not required, or configure an allowed real provider outside the repository.

`blocked_missing_vlm_provider`: visual artifacts exist, but no allowed VLM path can analyze them. Use explicit `codex-interactive` observation handoff, `manual-visual-review`, or an environment-configured `openai-responses-vision` run with real-provider permission. Do not claim that the runner can call hidden Codex interactive VLM APIs.

Policy blocks: robots, copyright, paywall, access control, CAPTCHA, PII, private images, unknown image license, and high-risk medical/legal/financial claims are preserved as policy flags. Guardrail-blocked claims cannot be accepted, promoted, or included in confident report findings until the required review/source support exists.

Budget pruning: source, image, screenshot, PDF, subagent, model-call, or cost caps can mark work as pruned. Inspect `budget_estimate.json`, `visual_provider_status.json`, `verification_matrix_status.json`, and `report_status.json`. Pruned claims should carry `include_in_final_report=false` and stay out of final confident findings.

Provider-gated real runs: real image search, browser/PDF acquisition, and Responses API vision can require credentials, operator confirmation, rate-limit headroom, and provider terms review. Keep secret values in the environment or a local secret manager, never in docs, commits, generated bundles, or issue comments.

## Provider Configuration

`codex-plugin` search handoff uses the current Codex session. The runner records `search_tasks.json` and expects explicit `search_results.jsonl`; it does not call Codex-native search through a hidden library API.

`brave-image-search` is the current real web image search adapter shape. It requires a Brave Search API key in `CODEX_DEEPRESEARCH_BRAVE_SEARCH_API_KEY` or `BRAVE_SEARCH_API_KEY`, plus the non-secret confirmation `CODEX_DEEPRESEARCH_BRAVE_ALLOW_RESULT_STORAGE=true` after the operator has verified their plan allows storing result metadata. Missing key, missing storage confirmation, provider unavailability, or rate limits should produce `blocked_missing_visual_provider` diagnostics instead of fixture candidates.

`openai-responses-vision` real VLM analysis requires `--provider-mode real`, `--allow-real-vlm` or `CODEX_DEEPRESEARCH_OPENAI_RESPONSES_VISION_ALLOW_REAL=1`, and an API key in `OPENAI_API_KEY` or `CODEX_DEEPRESEARCH_OPENAI_API_KEY`. Missing permission or credentials should produce `blocked_missing_vlm_provider` while preserving fetched visual artifacts when policy allows.

Local providers such as `local-page`, `local-image-fixture`, `local-screenshot-fixture`, and `local-pdf-rasterizer` are deterministic validation aids. They should record `provider_mode=fixture` and should not be described as release-eligible automatic visual evidence.

## Example Gallery

Text-heavy fixture validation:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch invoke \
  '$deep-research: summarize the public architecture evidence for this repository' \
  --route text_only \
  --adapter fixture \
  --runs-dir /tmp/codex-deepresearch-example-text
```

This is deterministic development validation. It is not a real-use E2E pass.

Visual-required local fixture mechanics:

```bash
tmpdir=/tmp/codex-deepresearch-example-visual
rm -rf "$tmpdir"
run_dir=$(plugins/codex-deepresearch/scripts/codex-deepresearch prepare \
  "Validate visual acquisition mechanics" \
  --runs-dir "$tmpdir" \
  --route visual_required \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_dir"])')
plugins/codex-deepresearch/scripts/codex-deepresearch acquire-visual \
  --run "$run_dir" \
  --provider local-page \
  --provider local-image-fixture \
  --provider local-screenshot-fixture \
  --screenshot-mode all
plugins/codex-deepresearch/scripts/codex-deepresearch ingest-vision \
  --run "$run_dir" \
  --provider codex-interactive
```

This proves local artifact and schema mechanics only. It is fixture/local provenance, not release-eligible automatic visual research.

Mixed visual/text local workflow:

```bash
tmpdir=/tmp/codex-deepresearch-example-mixed
rm -rf "$tmpdir"
run_dir=$(plugins/codex-deepresearch/scripts/codex-deepresearch prepare \
  "Compare the text claims and visible page evidence for a public source" \
  --runs-dir "$tmpdir" \
  --route visual_optional \
  --angle "text claims from the public page" \
  --angle "visible page images and screenshots" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_dir"])')
plugins/codex-deepresearch/scripts/codex-deepresearch ingest-manual \
  --run "$run_dir" \
  --url https://example.com/source \
  --image-url https://example.com/image.png
plugins/codex-deepresearch/scripts/codex-deepresearch acquire-visual \
  --run "$run_dir" \
  --provider local-page \
  --provider local-screenshot-fixture
plugins/codex-deepresearch/scripts/codex-deepresearch ingest-vision \
  --run "$run_dir" \
  --provider manual-visual-review
```

This demonstrates mixed text and visual artifact plumbing with manual/user-provided plus local fixture provenance. It is not a no-user-image real-provider run.

Manual or user-provided evidence:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch ingest-manual \
  --question "Review the supplied public source" \
  --url https://example.com/source \
  --image-url https://example.com/image.png
```

Manual and user-provided paths are useful for review workflows and fallback runs. They do not satisfy the no-user-image Public Beta automatic visual gate.

Evidence review:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch browse-evidence --run <run_id_or_path>
plugins/codex-deepresearch/scripts/codex-deepresearch review-claim \
  --run <run_id_or_path> \
  --claim-id <claim_id> \
  --decision needs_more_evidence
plugins/codex-deepresearch/scripts/codex-deepresearch reuse-evidence --run <run_id_or_path>
```

Report templates and exports:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch synthesize --run <run_id_or_path>
plugins/codex-deepresearch/scripts/codex-deepresearch export-report \
  --run <run_id_or_path> \
  --template technical_report \
  --format all
```

Release-eligible real-provider visual run shape:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch acquire-visual \
  --run <run_id_or_path> \
  --provider brave-image-search
plugins/codex-deepresearch/scripts/codex-deepresearch ingest-vision \
  --run <run_id_or_path> \
  --provider openai-responses-vision \
  --provider-mode real \
  --allow-real-vlm
```

Only use this shape when the required provider credentials, storage-policy confirmation, and VLM permission are configured outside the repository. A release-eligible automatic visual run must record non-fixture provider provenance and cost metadata, reach `completed_auto_visual`, and cite at least one supported visual or mixed claim in `report.md`.

## Public Repo Safety

Do not commit or publish credentials, `.env` files, local Codex sessions, private screenshots, raw browser profiles, provider response dumps containing account data, or generated evidence bundles containing personal data. Example docs should use public URLs such as `https://example.com/...` and placeholders for run IDs, claim IDs, and provider configuration.
