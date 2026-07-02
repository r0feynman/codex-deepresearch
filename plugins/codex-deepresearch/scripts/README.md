# Plugin Scripts

`codex-deepresearch` is the plugin-local developer runner. It validates the plugin scaffold, starts smoke run directories, normalizes execution-mode configuration, and manages Codex-native search handoff and manual-source artifacts.

For the end-to-end local plugin install/update/remove flow, first-run verification, troubleshooting, and beta example gallery, see `../../../docs/codex-deepresearch-plugin-guide.md`.

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

## Automated Visual E2E

`automated-visual-e2e` is the P3-AV9 automated-cli real-provider visual release gate. It validates existing no-user-image visual-required runs for product/image discovery, UI screenshot comparison, public chart/report visual extraction, and public PDF/paper figure extraction. Passing runs must use real acquisition provider artifacts plus `openai-responses-vision`, reach `completed_auto_visual`, include at least 10 real image-centric candidates, include at least 3 real VLM-analyzed images, and cite at least one supported visual or mixed claim in `report.md`.

Credential-free environments can record blocked diagnostics without making provider calls:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch automated-visual-e2e \
  --runs-dir /tmp/codex-deepresearch-automated-visual-e2e \
  --suite-id automated-visual-e2e \
  --clean \
  --allow-blocked
```

To run the strict gate after producing real scenario artifacts, pass each scenario run explicitly:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch automated-visual-e2e \
  --runs-dir /tmp/codex-deepresearch-automated-visual-e2e \
  --suite-id automated-visual-e2e \
  --clean \
  --scenario-run product_image_discovery=/path/to/product-run \
  --scenario-run ui_screenshot_comparison=/path/to/screenshot-run \
  --scenario-run public_chart_report_visual_extraction=/path/to/chart-run \
  --scenario-run public_pdf_paper_figure_extraction=/path/to/pdf-run
```

The results file classifies provider, fetch, policy, VLM, contradiction, and report-linkage failures. Fixture, local test, manual-review, and user-provided-only visual evidence are reported but excluded from release numerator counts.

## Fresh Session Visual E2E

`fresh-session-visual-e2e` is the P3-AV8 public-safe visual gate. It keeps fixture/manual/user-provided visual evidence out of release-gate passes, records a blocked status when a visual-required prompt lacks a real visual/VLM provider, and only sets `release_gate_passed=true` for `completed_auto_visual` runs with at least 3 real Codex-interactive analyzed images and at least 1 report-cited visual or mixed claim.

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch fresh-session-visual-e2e \
  --runs-dir /tmp/codex-deepresearch-fresh-session-visual-e2e \
  --suite-id fresh-session-visual-e2e \
  --clean \
  --real-codex-interactive skip
```

The default `skip` mode is deterministic and CI-safe: the command exits successfully when the harness, transcript artifact exposure, fixture exclusion, and blocked-provider diagnostics are valid, but the result remains `release_gate_passed=false` with `release_gate_status=blocked_public_safe`. Use `--completed-auto-visual-run <run-dir-or-run_status.json>` with `--real-codex-interactive=require` for a strict local gate that fails unless real Codex-interactive visual capability produces validated `completed_auto_visual` artifacts.

## Public Beta Validation

`public-beta-validation` is the P3-E2E1 real-use validation classifier. It loads the curated public-safe prompt manifest at `plugins/codex-deepresearch/validation/public_beta_prompts.json`, verifies that it covers at least 20 prompts with at least 8 visual-required or visual-optional prompts, and writes sanitized `public_beta_validation_results.json` plus `public_beta_validation_summary.md`.

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch public-beta-validation \
  --runs-dir /tmp/codex-deepresearch-public-beta-validation \
  --suite-id public-beta-validation \
  --clean \
  --allow-blocked
```

When real Codex-native run artifacts are unavailable, the command records explicit blocked diagnostics and exits zero only with `--allow-blocked`; blocked runs are counted separately from failed non-blocked runs and never satisfy release readiness or issue #75 completion. To classify sanitized real runs, pass repeated `--run prompt_id=/path/to/run-dir` values. Each supplied run must be fresh, public-safe, bound to the evaluated prompt by prompt id plus original question or prompt hash, bound to the validation suite id, and internally consistent across run status, evidence, report status, and visual provider status. Visual prompts also require non-fixture Codex-native acquisition evidence, Codex-interactive VLM handoff observations, and report-cited supported visual or mixed claims. To attach existing gate artifacts from `fresh-session-e2e`, `fresh-session-visual-e2e`, or `automated-visual-e2e`, pass repeated `--gate-result gate_id=/path/to/results.json` values. External gate artifacts must declare their schema version, `public_safe=true`, a fresh `generated_at`, explicit release success, and required outcome counts or thresholds before they can pass as diagnostics. The classifier stores prompt IDs, status artifact paths, provider provenance summaries, failure categories, release-gate readiness, and remaining gaps; it does not copy raw evidence bundles, credentials, non-public screenshots, or personal data. The public Markdown summary uses prompt-scoped references instead of private absolute run paths.

Ad hoc `invoke` runs are still valid for normal local research, but they are not release-validation runs unless they are launched with identity flags before child execution starts. For Public Beta signoff, invoke the exact manifest prompt text and pass `--prompt-id` plus `--suite-id`; `--prompt-hash` is optional because the runner computes the canonical hash from `--original-question` or the normalized invocation question.

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch invoke \
  '$deep-research: <manifest prompt text>' \
  --runs-dir /tmp/codex-deepresearch-public-beta-runs \
  --route text_only \
  --prompt-id pb-text-001 \
  --suite-id public-beta-validation
```

Release-validation `invoke` writes `prompt_id`, `suite_id`, `prompt_hash`, `original_question`, `execution_mode=codex-plugin`, and `runner_mode=full-runner` to the initial `run_status.json` and `evidence.json` before parallel child work starts. Downstream status artifacts such as `report_status.json`, `visual_provider_status.json`, and `visual_search_plan.json` preserve the same identity when they are present. Codex-native child search handoff records are normalized into `search_results.jsonl` with `provider=codex-native`, `provider_mode=real`, `retrieval_status=fetched`, `policy_decision=allowed`, matching prompt identity, and no hidden API marker fields.

By default `--completion-mode codex-native` treats issue #75 as complete when all 20 supplied runs are `codex-plugin` runs with Codex-native `search_tasks.json`/`search_results.jsonl` handoff artifacts, at least 8 visual prompt runs reach `completed_auto_visual` through real Codex-native visual candidate/fetch artifacts plus explicit `codex-interactive` `visual_observations.jsonl` handoff records, and the report cites supported visual or mixed claims. The runner does not call hidden Codex search or VLM APIs. `--gate-result` artifacts, including `automated-visual-e2e` and `openai-responses-vision` diagnostics, are validated and reported separately in this mode. Use `--completion-mode external-gated` only when the release decision intentionally requires every supported external gate artifact to be supplied and pass as well; the `automated_cli_real_provider_visual_e2e` gate is satisfied by its external gate result JSON rather than by prompt-manifest metrics.

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

For handoff-only release-validation setup, `prepare` accepts the same `--prompt-id`, `--suite-id`, optional `--prompt-hash`, and optional `--original-question` identity flags. This stamps the prepared artifacts, but a run still must be completed through the full runner before `public-beta-validation` can count it as a passing supplied run.

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

Use `pause-run` to persist a durable pause gate without deleting or rewriting evidence artifacts. While paused, stage commands fail before starting new work and `run-status` keeps reporting the next safe stage for resume:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch pause-run --run <run_id_or_path> --reason "operator pause"
```

Use `resume-run` to clear the pause gate. Resume does not rerun completed stages by itself; continue with the reported `next_safe_stage` command so existing run steps and cache artifacts remain authoritative:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch resume-run --run <run_id_or_path>
```

Use `cancel-run` to record terminal cancellation diagnostics. Cancellation writes `run_control.json`, updates `run_steps.json`, writes terminal `run_status.json`, enumerates known child contexts from `research_tasks.json`, `subagent_assignments.jsonl`, and `run_trace.jsonl`, and records requested/attempted close records. It does not delete evidence, source, visual, shard, or report artifacts:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch cancel-run --run <run_id_or_path> --reason "operator cancel"
```

Most completed stages rerun and revalidate their inputs until M15 cache keys exist. Stages that explicitly skip a completed rerun keep the primary stage state as `completed` and record the skipped rerun in `run_steps.json` history plus `run_trace.jsonl`.

## Run Monitor

Use `monitor-list` for a compact read-only dashboard of run phase, shard buckets, provenance mode, evidence counts, and budget confirmation state:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch monitor-list --runs-dir research-runs
```

Use `monitor-detail` to inspect one run without opening implementation files:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch monitor-detail --run <run_id_or_path>
```

The monitor reads existing status artifacts only: `run_status.json`, `status.json`, `run_steps.json`, `run_control.json`, `parallel_orchestration_status.json`, `research_tasks.json`, `subagent_assignments.jsonl`, `merge_status.json`, `run_trace.jsonl`, visual status artifacts, `evidence.json`, and `budget_estimate.json`. Compact shard output is ordered as queued/active/completed/failed/accepted/merged/retried/blocked. Paused and cancelled runs remain visible in list and detail views, with detail output showing the control action and child-close record counts. List output uses run IDs and paths relative to `--runs-dir`; detail output may show the selected run directory but renders nested artifacts run-relative or as `<outside-run-dir>` when a status payload points elsewhere. Pass `--json` to either command for deterministic machine-readable output.

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

Use `ingest-vision` after visual tasks have produced fetched visual artifacts or a JSONL handoff artifact. When `--observations` is supplied, the command stays local/dry: it reads those observation records and normalizes them into `VisualEvidence` without calling Codex, OpenAI, or any external network service.

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch ingest-vision \
  --run <run_id_or_path> \
  --provider codex-interactive \
  --observations ./path/to/visual-observations.jsonl
```

`--provider` must be one of `codex-interactive`, `openai-responses-vision`, or `manual-visual-review`. With fetched local visual artifacts and no `--observations`, `codex-interactive` uses the Codex CLI image worker handoff (`codex exec --json --image <artifact>`) and records Codex-native visual observations without calling a hidden VLM API. `openai-responses-vision` can ingest a deterministic response fixture or, when `--observations` is omitted, analyze fetched `visual_candidates.jsonl` / `image_fetch_status.jsonl` artifacts through the Responses API only when `--provider-mode real`, `--allow-real-vlm` or `CODEX_DEEPRESEARCH_OPENAI_RESPONSES_VISION_ALLOW_REAL=1`, and `OPENAI_API_KEY` or `CODEX_DEEPRESEARCH_OPENAI_API_KEY` are configured. If the selected VLM path is unavailable, the command writes `blocked_missing_vlm_provider` and preserves fetched visual artifacts. `manual-visual-review` records human-entered observations with `analysis_provider=manual-visual-review`.

When `--observations` is omitted and no automated provider can analyze fetched artifacts, the command reads `visual_observations.jsonl` inside the run directory. If a `visual_required` route has no visual result, the command appends a low-confidence visual claim with `verification_status=needs_visual_evidence` so the gap remains schema-valid and explicit.

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

## Evidence Review

Use `browse-evidence` after verification and optional synthesis to inspect the local provenance graph for claims:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch browse-evidence \
  --run <run_id_or_path> \
  --claim-id <claim_id>
```

The JSON output links each claim to source records, quote spans, image artifacts, visual observations, verifier votes, report status entries, and reuse blockers. It reads only local artifacts and does not call models or the network.

Use `review-claim` to persist a local human decision:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch review-claim \
  --run <run_id_or_path> \
  --claim-id <claim_id> \
  --decision accepted
```

Supported decisions are `accepted`, `rejected`, and `needs_more_evidence`. The command writes claim fields in `evidence.json` plus `review_status.json`. Accepted claims become reuse-eligible only when `verification_status=supported` and guardrail/source/image policy blockers are absent. Guardrail-blocked claims cannot be accepted or promoted; rejected claims are excluded from later synthesis for the reviewed evidence version.

Use `reuse-evidence` to list claims currently eligible for downstream Codex reuse or promotion:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch reuse-evidence --run <run_id_or_path>
```

Human review decisions store a `review_evidence_cache_key`. If supporting source, image, quote, or visual-support inputs change, a later `verify-claims` run can mark the old human review stale and re-evaluate the claim.

## Synthesize Report

Use `synthesize` after claims have been verified:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch synthesize --run <run_id_or_path>
```

The command reads only the local `evidence.json` artifact and writes `report.md` plus `report_status.json`. It does not call external web, model, VLM, or API services. Confirmed report findings must have `verification_status=supported` and `review_status=auto_reviewed` or `human_accepted`; `include_in_final_report=false` is honored as an additional exclusion guard.

High-confidence text findings require a source-linked quote span. Visual and mixed findings require supporting image evidence IDs that resolve to non-policy-blocked `VisualEvidence`. Unsupported, refuted, policy-blocked, budget-pruned, unverified, and otherwise under-evidenced claims are kept out of the confident findings and recorded in the status manifest and the report's excluded evidence section.

## Export Report

Use `export-report` after `synthesize` has written local `evidence.json`, `report.md`, and `report_status.json`:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch export-report \
  --run <run_id_or_path> \
  --template technical_report \
  --format all \
  --output-dir /tmp/codex-deepresearch-report-export
```

Templates are `technical_report`, `market_report`, `competitor_analysis`, and `incident_report`; short aliases `technical`, `market`, `competitor`, and `incident` are accepted. Formats are `markdown`, `json`, `csv`, `html`, or `all`; `--format` may be repeated or comma-separated. HTML writes a small bundle directory with `index.html` and `manifest.json`.

Exports read only local artifacts and do not call network, model, search, or VLM services. All templates use the same supported evidence model as `synthesize`: unsupported, rejected, policy-blocked, and not-eligible claims are excluded by default. Pass `--include-excluded-caveats` only when the review bundle should include those claims in an explicit excluded/caveated section or row. JSON exports include report status, claim IDs, used source IDs, used image IDs, caveats, source metadata, and image appendix metadata. CSV exports include one review row per included claim, plus excluded/caveated rows only when requested.

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
