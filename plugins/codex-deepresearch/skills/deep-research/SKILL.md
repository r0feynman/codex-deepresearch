---
name: deep-research
description: Run a Codex DeepResearch workflow for questions that need multi-source research, evidence extraction, claim verification, or visual/VLM analysis from images, screenshots, charts, UI, or web pages.
---

# Deep Research

Use this skill when the user asks for deep research, source-backed investigation, competitive analysis, incident/background research, technical comparison, or image-backed evidence gathering.

## Workflow

1. Restate the research question and identify constraints.
2. Split the task into 5-8 research angles.
3. Classify each angle as:
   - `text_only`: text sources are sufficient.
   - `visual_optional`: images or screenshots may improve confidence.
   - `visual_required`: visual evidence is necessary.
4. For text angles, collect source metadata, extract claims, and preserve citations.
5. For visual angles, collect image candidates, page screenshots, surrounding text, alt text, captions, OCR output when available, and source URLs.
6. Use VLM analysis for visual evidence when image files or screenshots are available in the workspace or can be collected through available tools.
7. Verify major claims with independent sources. Mark conflicts instead of hiding them.
8. Produce a report with:
   - direct answer
   - evidence summary
   - confidence level
   - unresolved questions
   - source list
   - visual appendix when visual evidence was used

## Codex Plugin Handoff

In plugin mode, do not assume a hidden Codex search API is available to the runner. Use explicit handoff artifacts:

1. Run `plugins/codex-deepresearch/scripts/codex-deepresearch prepare "<question>"`, passing repeated `--angle "<planner angle>"` values when planner angles are already known.
2. Read the generated `search_tasks.json`.
3. Use the current Codex session's available search capability to perform each task.
4. Write one `SearchResult` JSON object per line to `search_results.jsonl`, preserving task id, angle id, route, provider, query, URL, title, snippet, result type, rank, freshness, access time, policy decision, policy flags, and raw provider metadata.
5. Run `plugins/codex-deepresearch/scripts/codex-deepresearch ingest --run <run_id_or_path>`.
6. For visual tasks, write explicit visual observation JSONL and run `plugins/codex-deepresearch/scripts/codex-deepresearch ingest-vision --run <run_id_or_path> --provider <codex-interactive|openai-responses-vision|manual-visual-review> --observations <jsonl>`. Do not assume the runner can call Codex interactive VLM as a hidden API.
7. Run `plugins/codex-deepresearch/scripts/codex-deepresearch fetch-claims --run <run_id_or_path>` to fetch queued sources, preserve source artifacts, extract quote candidates, and append low-confidence unverified claims.
8. Run `plugins/codex-deepresearch/scripts/codex-deepresearch verify-claims --run <run_id_or_path>` to apply the deterministic verifier matrix, write `verifier_votes.jsonl`, and update claim verification state. This runner stage uses only normalized local evidence; it does not call external models, VLMs, web search, or APIs.
9. Run `plugins/codex-deepresearch/scripts/codex-deepresearch synthesize --run <run_id_or_path>` to write `report.md` and `report_status.json` from supported, reviewed claims only. This runner stage uses only local evidence and must not call external models, VLMs, web search, or APIs.
10. Continue only from the normalized `evidence.json`, `fetch_queue.json`, `visual_observations.jsonl`, `verifier_votes.jsonl`, fetched source artifacts, `report.md`, and `report_status.json`.

## Manual Sources Fallback

When the user provides URLs, PDF URLs, image URLs, or local image files directly, or when Codex-native search handoff is blocked, do not call external search. Use the manual source path:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch ingest-manual --question "<question>" --url <url>
```

Use `--pdf` for PDF URLs or local PDF files, `--image-url` for remote images, and `--local-image` for local image files. For new manual runs, `ingest-manual` creates `evidence.json` with `mode=manual-sources` and `search_provider=manual`. When `--run` points at an existing run, it appends source records and `VisualEvidence` for image inputs while preserving that run's existing mode, providers, routing, and search tasks. It records metadata only: no remote body fetch, claim extraction, verification, VLM analysis, or report generation happens in this fallback slice.

## Evidence Rules

- Prefer primary sources, official documentation, original reports, papers, repositories, or direct screenshots.
- Do not treat a search result snippet as evidence by itself.
- Treat first-pass fetched text claims as unverified and low confidence until later verifier stages review them.
- Apply route-specific verifier rules before promotion or report drafting: `text_only` claims require two text votes and one policy/freshness vote, `visual_required` claims require two text votes, one visual vote, and one policy vote, and `visual_optional` claims use visual votes only when usable visual evidence is already available.
- Keep `budget_pruned` claims out of final reporting by honoring `include_in_final_report=false`.
- Report only claims with `verification_status=supported` and `review_status=auto_reviewed` or `human_accepted`; treat `include_in_final_report=false` as an additional exclusion guard.
- High-confidence text report claims must preserve quote spans with source IDs, and visual or mixed report claims must preserve supporting image evidence IDs.
- Track retrieval date for time-sensitive facts.
- Separate observations from inference.
- For images, preserve the original page URL when possible, not only the image URL.

## Output Shape

Use concise sections:

```text
Answer
Evidence
Visual Findings
Conflicts Or Gaps
Sources
Next Steps
```

Skip `Visual Findings` when no visual evidence was used.
