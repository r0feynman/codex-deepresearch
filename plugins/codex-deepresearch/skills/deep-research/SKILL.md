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

## Evidence Rules

- Prefer primary sources, official documentation, original reports, papers, repositories, or direct screenshots.
- Do not treat a search result snippet as evidence by itself.
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
