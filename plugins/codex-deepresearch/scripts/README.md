# Plugin Scripts

`codex-deepresearch` is the plugin-local developer runner. It validates the plugin scaffold, starts smoke run directories, normalizes execution-mode configuration, and manages Codex-native search handoff and manual-source artifacts.

## Smoke

From the repository root:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch smoke --install --invoke '$deep-research: Codex DeepResearch smoke test'
```

The command checks the manifest, repo-local marketplace metadata, and Codex CLI availability when `--install` is passed. It writes a timestamped directory under `research-runs/` with `status.json`.

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

## Vision Adapter

Use `ingest-vision` after visual tasks have produced a JSONL handoff artifact. The command is a dry adapter: it reads local observation records and normalizes them into `VisualEvidence` without calling Codex interactive VLM, OpenAI, or any external network service.

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch ingest-vision \
  --run <run_id_or_path> \
  --provider codex-interactive \
  --observations ./path/to/visual-observations.jsonl
```

`--provider` must be one of `codex-interactive`, `openai-responses-vision`, or `manual-visual-review`. `codex-interactive` observations are expected to come from explicit JSONL written by the Codex-side agent. `openai-responses-vision` can ingest a deterministic response fixture or artifact and records `analysis_provider=openai-responses-vision`; it does not perform a real API call. `manual-visual-review` records human-entered observations with `analysis_provider=manual-visual-review`.

When `--observations` is omitted, the command reads `visual_observations.jsonl` inside the run directory. If a `visual_required` route has no visual result, the command appends a low-confidence visual claim with `verification_status=needs_visual_evidence` so the gap remains schema-valid and explicit.

## Fetch Claims

Use `fetch-claims` after `ingest` has produced normalized sources and `fetch_queue.json`:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch fetch-claims --run <run_id_or_path>
```

The command fetches queued `http`, `https`, `file`, or `data` sources with an explicit timeout, writes fetched artifacts under the run directory, extracts text excerpts and quote candidates, and appends source-linked text claims to `evidence.json`. First-pass claims are intentionally conservative: `confidence=low`, `verification_status=unverified`, `review_status=not_reviewed`, and `promotion_status=not_eligible`. Blocked, manual-review, and failed sources remain preserved with failed retrieval metadata and do not create claims.

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
