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
