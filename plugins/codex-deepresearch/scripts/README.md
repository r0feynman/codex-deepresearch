# Plugin Scripts

`codex-deepresearch` is the plugin-local developer runner. It validates the plugin scaffold, starts smoke run directories, normalizes execution-mode configuration, and manages Codex-native search handoff and manual-source artifacts.

## Smoke

From the repository root:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch smoke --install --invoke '$deep-research: Codex DeepResearch smoke test'
```

The command checks the manifest, repo-local marketplace metadata, and Codex CLI availability when `--install` is passed. It writes a timestamped directory under `research-runs/` with `status.json`.

## MVP Smoke

`mvp-smoke` is the M12 release-gate suite. It is deterministic and no-network: fixtures use local HTML, local PNG bytes, fixture claims, and existing runner stages without live web, model, API, or VLM calls.

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch mvp-smoke \
  --runs-dir /tmp/codex-deepresearch-mvp-smoke \
  --suite-id mvp-smoke \
  --clean \
  --invoke '$deep-research: MVP smoke text-only fixture'
```

The suite writes `mvp_smoke_results.json` and per-fixture run directories containing `evidence.json`, `report.md`, verifier votes, and stage status files. It covers 3 text-only fixtures, 3 visual-required fixtures, 2 visual-optional fixtures, local plugin install/update checks, schema-v0 evidence validation, and the guardrail fixture suite.

By default, `mvp-smoke` requires the `codex` CLI on `PATH` so the plugin install/update capability is actually available. If an environment cannot provide Codex CLI, pass `--skip-codex-cli-install-check`; the results file will record `install_update_smoke.status=skipped`, `skips.codex_cli_install_check=true`, and `acceptance.plugin_install_update_smoke_passes=false` instead of claiming that the install/update smoke passed.

The invocation smoke also validates that `--invoke` starts with `$deep-research:`. Invalid invocations fail the suite and still write `mvp_smoke_results.json` with failed check details.

## Fresh Session E2E

`fresh-session-e2e` is the P3-UX3 scripted transcript gate. It invokes the plugin-local runner through `$deep-research: <question>` semantics, renders a fresh-session assistant response, and fails if a successful-looking response omits the run directory, `run_status.json`, or synthesized-run `report_status.json`.

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch fresh-session-e2e \
  --runs-dir /tmp/codex-deepresearch-fresh-session-e2e \
  --suite-id fresh-session-e2e \
  --clean \
  --real-codex-exec skip
```

The gate scenarios cover fixture full-runner completion, serial fallback as an explicit blocked state, and a real `codex-exec` scenario. Use `--real-codex-exec=skip` for CI/no-private-artifact validation; it records a blocked diagnostic instead of pretending real child execution occurred. Use `--real-codex-exec=require` for a local strict gate that fails unless real `codex-exec` produces `accepted_shards > 0`.

The CLI default is public-safe `--real-codex-exec=skip`. Use `--real-codex-exec=auto` to attempt real child execution when Codex CLI is available. Real and fixture scenarios are bounded by `--scenario-timeout-seconds` (default `120`); auto mode records a timeout as an explicit blocked diagnostic, while require mode fails unless real execution completes with accepted shards.

## Resolve Config

Use `resolve-config` to normalize execution mode, provider flags, and budget preset before runner work starts:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch resolve-config --mode codex-plugin --search-provider codex-native --budget-preset standard
```

The command prints deterministic JSON on success, including a normalized `budget_preset` field. `--budget` remains available as a shorter alias for `--budget-preset`. Invalid mode/provider combinations exit nonzero with a clear error.

## Search Handoff

Use `prepare` to create a plugin-mode run directory without calling any hidden Codex search API:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch prepare "What evidence is available?"
```

The command writes `evidence.json`, `search_tasks.json`, an empty `search_results.jsonl`, `visual_tasks.json`, and an empty `visual_observations.jsonl` under `research-runs/<run_id>/`. Codex should perform search in the active session and append one `SearchResult` JSON object per line to `search_results.jsonl`.

`prepare` also writes `budget_estimate.json` before any search, fetch, VLM, verifier, or subagent work starts. Use `--max-sources`, `--max-images`, `--max-subagents`, `--max-agents`, `--max-cost-usd`, and `--codex-runner codex-exec|codex-sdk|serial` to request lower caps; the estimate records deterministic reduction suggestions and the generated handoff files use the reduced source/image caps. `deep` and `exhaustive` presets require `--confirm-budget`; `exhaustive` also requires `--max-cost-usd`.

By default, `prepare` classifies each planner angle as `text_only`, `visual_required`, or `visual_optional` and records the route in `evidence.json.routing`. Pass repeated `--angle` values to supply planner angles:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch prepare \
  "Compare checkout flows for these products" \
  --angle "official API docs and release notes" \
  --angle "checkout UI screenshot comparison"
```

Use `--route text_only`, `--route visual_required`, or `--route visual_optional` only when all generated angles need an explicit override. Text-only routes write no visual tasks and set `max_images` to `0`.

Then ingest only the handoff artifact:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch ingest --run <run_id_or_path>
```

`ingest` validates `search_results.jsonl`, normalizes records into `evidence.json.sources`, and writes `fetch_queue.json` for allowed, fetchable `http`/`https` sources. Invalid URLs and blocked/manual-review policy decisions are preserved in evidence with `retrieval_status=failed` and explicit ingest errors, but they are not added to the fetch queue.

## Parallel Orchestration

Use `plan-parallel` to expand prepared planner output into the canonical M18 `research_tasks.json` queue:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch plan-parallel \
  --run <run_id_or_path> \
  --min-tasks 20
```

Use `orchestrate-parallel` to assign bounded tasks, collect per-task evidence shards, record normalized Codex subagent events, and merge schema-valid shards back into canonical `evidence.json`:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch orchestrate-parallel \
  --run <run_id_or_path> \
  --adapter codex-exec \
  --min-tasks 20
```

The `codex-exec` adapter constructs `codex exec --json --ignore-user-config --ignore-rules -C <project-root> --add-dir <run-dir> -c agents.max_threads=N -c sandbox_mode=workspace-write -c approval_policy=never` child runs. The trusted project root is used for Codex execution while prompts still write shards under the selected run directory. The child process ignores personal MCP/tool config and local rules so automated shard runs do not inherit unrelated auth prompts, while Codex auth still comes from the current CLI session. If Codex execution is unavailable and degradation is allowed, the runner records `parallel_degraded=true` and executes the same queue serially as blocked/degraded tasks without fabricating confident evidence. Tests and local smokes can select `--adapter fixture` directly; it is no-network/no-auth and writes representative `spawn_agent`, `wait`, `message`, and `close_agent` trace records plus schema-valid shards.

The standard preset uses up to 8 concurrent Codex subagents or equivalent worker contexts through a bounded local scheduler. The existing `prepare` budget gate keeps `exhaustive` behind `--confirm-budget` plus `--max-cost-usd`; direct `plan-parallel` and `orchestrate-parallel` calls also require `--confirm-exhaustive` and `--max-cost-usd` unless the prepared run's `budget_estimate.json` already records confirmation and a cost cap.

## Run Status

Every state-managed pipeline stage writes `run_steps.json` with `pending`, `running`, `completed`, `failed`, or `skipped` state. Use `run-status` to inspect an interrupted run and identify the next safe stage:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch run-status --run <run_id_or_path>
```

Most completed stages rerun and revalidate their inputs until M15 cache keys exist. Stages that explicitly skip a completed rerun keep the primary stage state as `completed` and record the skipped rerun in `run_steps.json` history plus `run_trace.jsonl`.

## Visual Acquisition

Use `acquire-visual` on `visual_required` or `visual_optional` runs to create deterministic local/test image candidates before `ingest-vision`:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch acquire-visual \
  --run <run_id_or_path> \
  --provider local-page \
  --provider local-image-fixture \
  --provider local-screenshot-fixture \
  --provider local-pdf-rasterizer \
  --screenshot-mode all
```

The command writes all candidates to `visual_candidates.jsonl` and selected handoff records to `visual_observations.jsonl`. It preserves candidate classes for Open Graph images, body images, image search results, screenshots, PDF pages, and PDF figure hints; records MIME, size, URL duplicate, content hash, perceptual hash, page/figure provenance, and near-duplicate checks; stores OCR/text-in-image fields separately from visual summary/description fields; and marks favicon, logo, thumbnail, tracking pixel, low-value preview, duplicate, policy-blocked, unsupported, too-large, encrypted, paywalled, and budget-pruned candidates with removal reasons. The local screenshot fixture represents first viewport, full-page, scroll, and interaction capture modes and records unsupported capabilities explicitly. The local PDF rasterizer reads only existing local PDF artifacts and emits deterministic public-safe PNG artifacts; it does not download PDFs, bypass paywalls, or decrypt protected files.

For automated CLI candidate discovery with a real image search provider, select `brave-image-search` and configure `CODEX_DEEPRESEARCH_BRAVE_SEARCH_API_KEY` or `BRAVE_SEARCH_API_KEY` in the environment. Brave's public Search API materials say result storage rights are plan/terms dependent, so persisted candidate discovery also requires the non-secret confirmation `CODEX_DEEPRESEARCH_BRAVE_ALLOW_RESULT_STORAGE=true` after the operator has confirmed their plan allows storing result metadata. Without that confirmation, the adapter blocks before making the provider request and writes `blocked_missing_visual_provider` diagnostics without persisted candidates.

```bash
export CODEX_DEEPRESEARCH_BRAVE_ALLOW_RESULT_STORAGE=true
plugins/codex-deepresearch/scripts/codex-deepresearch acquire-visual \
  --run <run_id_or_path> \
  --provider brave-image-search
```

Optional non-secret controls are `CODEX_DEEPRESEARCH_BRAVE_IMAGE_COUNT`, `CODEX_DEEPRESEARCH_BRAVE_COUNTRY`, `CODEX_DEEPRESEARCH_BRAVE_SEARCH_LANG`, `CODEX_DEEPRESEARCH_BRAVE_SAFESEARCH`, `CODEX_DEEPRESEARCH_BRAVE_TIMEOUT_SECONDS`, and `CODEX_DEEPRESEARCH_BRAVE_ESTIMATED_COST_USD`. The adapter writes `provider_mode=real`, `provider_kind=web_image_search`, source page URL, image URL, rank, score, policy state, cost fields, and sanitized provider diagnostics to `visual_candidates.jsonl`; it does not fetch image bytes or create VLM observations. Provider status persists sanitized diagnostics, rate-limit headers, external-network flags, config key names, and cost counters. If the real provider is requested but credentials, storage confirmation, or availability are missing, the command writes `blocked_missing_visual_provider` diagnostics and does not fabricate local fixture candidates.

References: Brave Search API FAQ, https://brave.com/search/api/; Brave Search API Terms of Service, https://api-dashboard.search.brave.com/terms-of-service.

`text_only` routes are an explicit no-op: the command writes empty visual candidate/observation artifacts and records zero image search, screenshot, OCR, and VLM work.

## Vision Adapter

Use `ingest-vision` after visual tasks have produced a JSONL handoff artifact. By default it reads local observation records and normalizes them into `VisualEvidence` without calling Codex interactive VLM, OpenAI, or any external network service.

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch ingest-vision \
  --run <run_id_or_path> \
  --provider codex-interactive \
  --observations ./path/to/visual-observations.jsonl
```

`--provider` must be one of `codex-interactive`, `openai-responses-vision`, or `manual-visual-review`. `codex-interactive` observations are expected to come from explicit JSONL written by the Codex-side agent. `openai-responses-vision` can ingest a deterministic response fixture or, when `--observations` is omitted, analyze fetched `visual_candidates.jsonl` / `image_fetch_status.jsonl` artifacts through the Responses API only when `--provider-mode real`, `--allow-real-vlm` or `CODEX_DEEPRESEARCH_OPENAI_RESPONSES_VISION_ALLOW_REAL=1`, and `OPENAI_API_KEY` or `CODEX_DEEPRESEARCH_OPENAI_API_KEY` are configured. If the VLM path is unavailable, the command writes `blocked_missing_vlm_provider` and preserves fetched visual artifacts. `manual-visual-review` records human-entered observations with `analysis_provider=manual-visual-review`.

When `--observations` is omitted, the command reads `visual_observations.jsonl` inside the run directory. If a `visual_required` route has no visual result, the command appends a low-confidence visual claim with `verification_status=needs_visual_evidence` so the gap remains schema-valid and explicit.

## Fetch Claims

Use `fetch-claims` after `ingest` has produced normalized sources and `fetch_queue.json`:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch fetch-claims --run <run_id_or_path>
```

The command fetches queued `http`, `https`, `file`, or `data` sources with an explicit timeout, writes fetched artifacts under the run directory, extracts text excerpts and quote candidates, and appends source-linked text claims to `evidence.json`. First-pass claims are intentionally conservative: `confidence=low`, `verification_status=unverified`, `review_status=not_reviewed`, and `promotion_status=not_eligible`. Blocked, manual-review, and failed sources remain preserved with failed retrieval metadata and do not create claims.

## Enforce Guardrails

Use `enforce-guardrails` after evidence, claims, and any visual records have been normalized and before verification or promotion:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch enforce-guardrails --run <run_id_or_path>
```

The command is fully local and deterministic. It reads `evidence.json`, preserves existing policy flags, adds guardrail flags for login/CAPTCHA/access-controlled sources, robots, paywall, copyright, PII, private user-provided images, unknown image licenses, and high-risk medical/legal/financial claims without primary-source support, then writes updated `evidence.json` and `guardrails_status.json`.

Guardrail-blocked evidence cannot remain `review_status=human_accepted` or `promotion_status=promoted_*`. Unknown-license images require human acceptance before eligibility, high-risk claims without primary-source support cannot remain high confidence, and policy-blocked claims receive `include_in_final_report=false`.

## Verify Claims

Use `verify-claims` after claims and any visual evidence have been normalized:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch verify-claims --run <run_id_or_path>
```

The command applies the PRD verifier matrix without external model, VLM, web, or API calls. It writes deterministic `runner-agent` `VerifierVote` records to `verifier_votes.jsonl`, embeds current votes on each claim, updates `verification_status`, `review_status`, and `promotion_status`, and writes `verification_matrix_status.json`. Route rules are enforced as `text_only` = two text votes plus one policy/freshness vote, `visual_required` = two text votes plus one visual vote plus one policy vote, and `visual_optional` = text and policy votes plus a visual vote only when usable visual evidence is already available.

Budget-pruned claims are not voted again. They keep `verification_status=budget_pruned` and receive `include_in_final_report=false` plus `report_exclusion_reason=budget_pruned` for the M10 report renderer to consume.

## Synthesize Report

Use `synthesize` after claims have been verified:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch synthesize --run <run_id_or_path>
```

The command reads only the local `evidence.json` artifact and writes `report.md` plus `report_status.json`. It does not call external web, model, VLM, or API services. Confirmed report findings must have `verification_status=supported` and `review_status=auto_reviewed` or `human_accepted`; `include_in_final_report=false` is honored as an additional exclusion guard.

High-confidence text findings require a source-linked quote span. Visual and mixed findings require supporting image evidence IDs that resolve to non-policy-blocked `VisualEvidence`. Unsupported, refuted, policy-blocked, budget-pruned, unverified, and otherwise under-evidenced claims are kept out of the confident findings and recorded in the status manifest and the report's excluded evidence section.

## Manual Sources

Use `ingest-manual` when the user provides URLs or images directly, or when Codex-native search handoff is blocked. The command does not call external search, fetch remote bodies, run VLM analysis, or create a fetch queue:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch ingest-manual \
  --question "What evidence is available?" \
  --url https://example.com/source \
  --pdf https://example.com/report.pdf \
  --image-url https://example.com/image.png \
  --local-image ./path/to/local-image.png
```

Without `--run`, `--question` is required and the command creates a `manual-sources` run under `research-runs/`. With `--run`, it appends manual records to an existing `evidence.json`. Page URLs and PDFs create source records; image URLs and local image files create both source records and `VisualEvidence`. Local images are copied into the run directory with deterministic MIME type, SHA-256 hash, and dimensions when common image headers expose them. Remote image URLs are recorded as metadata-only visual evidence with `width=0` and `height=0` because M5 intentionally does not fetch bytes.

## Install

The repo-local marketplace is `.agents/plugins/marketplace.json` and its marketplace name is `codex-deepresearch-local`.

```bash
codex plugin marketplace add .
codex plugin add codex-deepresearch@codex-deepresearch-local
```

Start a new Codex thread after installing so the `$deep-research` skill is loaded.

## Update

After changing plugin files, rerun validation and the smoke command:

```bash
python3 scripts/validate_repo.py
python3 /home/user/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/codex-deepresearch
plugins/codex-deepresearch/scripts/codex-deepresearch mvp-smoke --runs-dir /tmp/codex-deepresearch-mvp-smoke --suite-id mvp-smoke --clean --invoke '$deep-research: MVP smoke text-only fixture'
plugins/codex-deepresearch/scripts/codex-deepresearch fresh-session-e2e --runs-dir /tmp/codex-deepresearch-fresh-session-e2e --suite-id fresh-session-e2e --clean --real-codex-exec skip
tmpdir=/tmp/codex-deepresearch-parallel-validation; rm -rf "$tmpdir"; run_dir=$(plugins/codex-deepresearch/scripts/codex-deepresearch prepare "Parallel orchestration validation" --runs-dir "$tmpdir" --route text_only | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_dir"])'); plugins/codex-deepresearch/scripts/codex-deepresearch orchestrate-parallel --run "$run_dir" --adapter fixture --min-tasks 3
plugins/codex-deepresearch/scripts/codex-deepresearch smoke --install --invoke '$deep-research: Codex DeepResearch smoke test'
codex plugin add codex-deepresearch@codex-deepresearch-local
```

Start a new Codex thread after reinstalling.

## Remove

```bash
codex plugin remove codex-deepresearch@codex-deepresearch-local
```

To remove this repo marketplace from Codex as well:

```bash
codex plugin marketplace remove codex-deepresearch-local
```
